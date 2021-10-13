#include "lazy_tensor_core/csrc/ops/amp_foreach_non_finite_check_and_unscale.h"

#include "lazy_tensor_core/csrc/compiler/node_lowering.h"
#include "lazy_tensor_core/csrc/ops/ltc_ops.h"
#include "lazy_tensor_core/csrc/tensor_util.h"
#include "lazy_tensors/computation_client/util.h"
#include "lazy_tensors/shape_util.h"

namespace torch_lazy_tensors {
namespace ir {
namespace ops {
namespace {

lazy_tensors::Shape NodeOutputShape(const OpList& inputs,
                                    const Value& found_inf) {
  std::vector<lazy_tensors::Shape> output_shapes;
  output_shapes.reserve(inputs.size() + 1);
  for (size_t i = 0; i < inputs.size(); ++i) {
    const lazy_tensors::Shape& input_shape = GetShapeFromTsValue(inputs[i]);
    output_shapes.push_back(input_shape);
  }
  output_shapes.push_back(GetShapeFromTsValue(found_inf));
  return lazy_tensors::ShapeUtil::MakeTupleShape(output_shapes);
}

std::vector<Value> GetOperandList(lazy_tensors::Span<const Value> operands,
                                  const Value& found_inf,
                                  const Value& inv_scale) {
  std::vector<Value> operand_list(operands.begin(), operands.end());
  operand_list.push_back(found_inf);
  operand_list.push_back(inv_scale);
  return operand_list;
}

}  // namespace

AmpForachNonFiniteCheckAndUnscale::AmpForachNonFiniteCheckAndUnscale(
    const OpList& inputs, const Value& found_inf, const Value& inv_scale)
    : TsNode(ir::OpKind(at::aten::_amp_foreach_non_finite_check_and_unscale_),
           at::ArrayRef<Value>(GetOperandList(inputs, found_inf, inv_scale)),
           NodeOutputShape(inputs, found_inf),
           /*num_outputs=*/inputs.size() + 1) {}

NodePtr AmpForachNonFiniteCheckAndUnscale::Clone(OpList operands) const {
  std::vector<Value> operand_list(operands.begin(), operands.end() - 2);
  size_t sz = operand_list.size();
  return MakeNode<AmpForachNonFiniteCheckAndUnscale>(at::ArrayRef<Value>(operand_list), operands[sz],
                                                     operands[sz + 1]);
}

}  // namespace ops
}  // namespace ir
}  // namespace torch_lazy_tensors
