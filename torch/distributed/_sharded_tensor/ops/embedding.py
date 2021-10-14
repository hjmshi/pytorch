import torch
import torch.distributed as dist
from torch.distributed._sharding_spec import ChunkShardingSpec
from torch.distributed._sharding_spec._internals import (
    get_split_size,
    get_chunked_dim_size,
)


def sharded_embedding(types, args, kwargs, pg):
    """
    Handles ``__torch_function__`` dispatch for ``torch.nn.functional.embedding``.
    This method computes a sharded embedding lookup and has the following limitations:

    1. Supports only sharding of ``weight``.
    2. Supports only ``ChunkShardingSpec``.
    3. Supports only a single local shard per rank.
    4. Supports only simple look-up and including specs like padding_idx, max_norm, etc.
    5. Supports only column-wise sharding for now.

    Based on the dimension that the weight is sharded on, there are two
    algorithms:

    ROWWISE SHARDING
    ================
    For row-wise sharding the weight is sharded on dimension 0, but this has
    not been supported now and is in the process of implementation.

    COLWISE SHARDING
    ================
    For col-wise sharding the weight is sharded on dimension 1.

    The overall algorithm can be best explained with an example. Let's assume
    the dims for input are (4 x 6) and W are (16 x 17) and W is sharded across
    4 GPUs creating 3 shards of (16 x 5) and 1 shard of (16 x 2).
    The algorithm is as follows:

    1. First the input is broadcasted to all ranks, since this is SPMD we
       actually do an all_gather for all the inputs resulting in 4 (4 x 6)
       inputs on each rank.
    2. Next we perform local embedding look-up operation by apply each
       input (4 x 6) with the local shard (16 x 5) ((16 x 2) for the last).
       This results in 4 (5 x 6 x 4) ((2 x 6 x 4) for the last) matrices
       on each rank. We transpose dim 0 and dim 2.
    3. Next, we concat these 4 matrices and perform an all2all to share the
       appropriate (5 x 6 x 4) or (2 x 6 x 4) matrices to each rank.
    4. Now, each rank receives a (17 x 6 x 4) matrix which is basically the
       size of the result we need.
    5. If placements are not in order any appropriate rearrangement of columns
       are done for the (17 x 6 x 4) matrix and finally we transponse the
       dim 0 and dim 2 again.
    """
    from torch.distributed._sharded_tensor import ShardedTensor

    input = args[0]
    weight = args[1]

    # Validate types
    if not isinstance(input, torch.Tensor):
        raise TypeError("input need to be torch.Tensor")
    if not isinstance(weight, ShardedTensor):
        raise TypeError("weight needs to be ShardedTensor")
    if len(weight.size()) != 2:
        raise ValueError("Weight needs to have exactly 2 dims")
    if int(torch.min(input).item()) < 0:
        raise ValueError(
            "Index out of range in Input %d %d",
            int(torch.min(input).item()),
            weight.size()[1],
        )
    if int(torch.max(input).item()) >= weight.size()[0]:
        raise ValueError(
            "Index out of range in Input %d %d",
            int(torch.max(input).item()),
            weight.size()[1],
        )

    if not isinstance(weight._sharding_spec, ChunkShardingSpec):
        raise ValueError("Only ChunkShardingSpec supported for ShardedTensor ops!")
    if len(weight.local_shards()) != 1:
        raise ValueError("Only one local shard supported!")

    local_shard = weight.local_shards()[0].tensor.contiguous()
    sharding_dim = weight._sharding_spec.dim
    world_size = dist.get_world_size(pg)
    rank = dist.get_rank(pg)

    if sharding_dim == 1:
        return _handle_col_wise_sharding(input, world_size, weight, local_shard, pg)
    else:
        raise RuntimeError(
            f"nn.Embedding weight sharded on dim {sharding_dim} not supported!"
        )


def _handle_col_wise_sharding(input, world_size, weight, local_shard, pg):
    # allgather the inputs first.
    gathered_inputs = [torch.zeros_like(input) for _ in range(world_size)]
    dist.all_gather(gathered_inputs, input, group=pg)

    # run the embedding look-up for all the inputs.
    results = []
    for i, inp in enumerate(gathered_inputs):
        result = torch.nn.functional.embedding(inp, local_shard)
        results.append(torch.transpose(result, 0, len(input.size())))

    # Process inputs and outputs for all2all.
    sharding_dim_size = weight.size()[1]
    dims = list(results[0].size())
    dims[0] = sharding_dim_size
    output = torch.empty(*dims, device=input.device)
    combined_results = torch.cat(results)

    # Compute output splits
    split_size = get_split_size(sharding_dim_size, world_size)
    output_split_sizes = [int] * world_size
    for idx, placement in enumerate(weight._sharding_spec.placements):
        output_split_sizes[placement.rank()] = get_chunked_dim_size(
            sharding_dim_size, split_size, idx
        )

    # distribute the outputs using all2all.
    dist.all_to_all_single(
        output, combined_results, output_split_sizes=output_split_sizes, group=pg
    )

    # Check if we need to rearrange rows appropriately for output.
    rearrange_columns = any(
        [
            idx != placement.rank()
            for idx, placement in enumerate(weight._sharding_spec.placements)
        ]
    )
    if rearrange_columns:
        indices = []
        for placement in weight._sharding_spec.placements:
            dim_size = output_split_sizes[placement.rank()]
            start = sum(
                [
                    split_size if i < placement.rank() else 0
                    for i, split_size in enumerate(output_split_sizes)
                ]
            )
            indices += list(range(start, start + dim_size))

        output = output.index_select(0, torch.tensor(indices, device=output.device))

    # transpose the output and return result.
    return torch.transpose(output, 0, len(input.size()))
