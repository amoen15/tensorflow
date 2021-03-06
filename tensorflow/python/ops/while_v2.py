# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================
"""while_v2 and gradient.

This is a version of while_loop that emits a single While op, as well as the
gradient function for While ops produced by while_loop. This will eventually
replace the current tf.while_loop implementation once it reaches feature and
performance parity.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from tensorflow.core.framework import attr_value_pb2
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import func_graph as func_graph_module
from tensorflow.python.framework import function_def_to_graph
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_shape
from tensorflow.python.framework import tensor_spec
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import control_flow_util
from tensorflow.python.ops import control_flow_util_v2 as util
from tensorflow.python.ops import custom_gradient
from tensorflow.python.ops import gen_functional_ops
from tensorflow.python.ops import gradients_impl
from tensorflow.python.ops import list_ops
from tensorflow.python.ops import tensor_array_ops
from tensorflow.python.util import nest

# pylint: disable=protected-access

# TODO(b/79881896): Handle external control dependencies. tf.while_loop allows
# control dependencies on external nodes with at least 1 output.
# Another idea is to create const nodes outside the loop and add control edges
# to them and then pass those in as data inputs. This should probably be
# handled in the CapturingGraph itself.

# Op types that output a resource tensor representing a TensorArray handle.
TENSOR_ARRAY_HANDLE_OPS = (
    "TensorArrayV3",
    "TensorArrayGradV3",
    "TensorArrayGradWithShape",
)


def while_loop(cond, body, loop_vars, shape_invariants=None, name=None):
  """Like tf.while_loop, except emits a single While op."""
  # Keep the original loop_vars around to know which args were TensorArrays.
  orig_loop_vars = loop_vars
  # Cache its length since we use it at multiple places below.
  len_orig_loop_vars = len(orig_loop_vars)

  # Convert TensorArrays to their flow variables. These get converted back to
  # TensorArrays before calling `cond` and `body`. See `wrapped_cond` and
  # `wrapped_body` below.
  loop_vars = list(_tensor_array_to_flow(orig_loop_vars))
  loop_vars = nest.map_structure(
      ops.internal_convert_to_tensor_or_indexed_slices, loop_vars)
  if shape_invariants is not None:
    nest.assert_same_structure(orig_loop_vars, shape_invariants)
  else:
    shape_invariants = nest.map_structure(lambda t: t.shape, loop_vars)

  if not name:
    name = "while"

  with ops.name_scope(name) as scope:
    with ops.name_scope(None):
      cond_name = util.unique_fn_name(scope, "cond")
      body_name = util.unique_fn_name(scope, "body")

    # Add loop counter needed for computing gradients.
    loop_vars = [constant_op.constant(0., name="loop_counter")] + loop_vars

    shape_invariants = [tensor_shape.scalar()] + shape_invariants

    # Automatic control dependencies are added in defuns, but not in v1
    # graphs. Propagate that behavior here.
    add_control_dependencies = util.in_defun()

    # Build a `cond` wrapper that can handle the extra counter loop_var.
    def wrapped_cond(unused_loop_counter, *args):
      # Convert the flow variables in `args` to TensorArrays. `args` should
      # already have the same structure as `orig_loop_vars` but currently there
      # is no nest.zip so we call `_pack_sequence_as` which flattens both
      # `orig_loop_vars` and `args`, converts flows in `args` to TensorArrays
      # and packs it into the structure of `orig_loop_vars`.
      return cond(*_pack_sequence_as(orig_loop_vars, args))

    cond_graph = func_graph_module.func_graph_from_py_func(
        cond_name,
        wrapped_cond,
        loop_vars, {},
        signature=_build_signature(loop_vars, shape_invariants),
        func_graph=util.WhileCondFuncGraph(cond_name),
        add_control_dependencies=add_control_dependencies)

    # Add external_captures of cond to the list of loop vars.
    # Note that external tensors will be treated as loop invariants, i.e.,
    # the value of that tensor in each iteration is the same as it was at the
    # beginning of the loop execution.
    loop_vars = loop_vars + cond_graph.external_captures
    shape_invariants = shape_invariants + [
        t.shape for t in cond_graph.external_captures
    ]

    def wrapped_body(loop_counter, *args):
      """Loop body augmented with counter update.

      Args:
        loop_counter: Loop counter which needs to be incremented in the body.
        *args: List of args
          args[:len_orig_loop_vars] - Args for the original loop body.
          args[len_orig_loop_vars:] - External captures of cond. These get
            passed through as is.

      Returns:
        A list of tensors the same length as args.
      """
      # Convert the flow variables in `args` to TensorArrays. `args` should
      # already have the same structure as `orig_loop_vars` but currently there
      # is no nest.zip so we call `_pack_sequence_as` which flattens both
      # `orig_loop_vars` and `args`, converts flows in `args` to TensorArrays
      # and packs it into the structure of `orig_loop_vars`.
      outputs = body(
          *_pack_sequence_as(orig_loop_vars, args[:len_orig_loop_vars]))
      if not nest.is_sequence(outputs):
        outputs = [outputs]
      # Compare the structure of input and output of body converting the
      # top-level tuples to list to be compatible with legacy while_loop.
      nest.assert_same_structure(list(outputs), list(orig_loop_vars))

      outputs = _tensor_array_to_flow(outputs)

      # Return the external_captures of cond_graph as is, i.e., treat them as
      # loop invariants.
      # TODO(srbs): Update lowering code to create _Enter nodes with
      # is_constant=True for inputs that are directly passed to outputs.
      return [loop_counter + 1] + list(outputs) + list(
          args[len_orig_loop_vars:])

    body_graph = func_graph_module.func_graph_from_py_func(
        body_name,
        wrapped_body,
        loop_vars, {},
        signature=_build_signature(loop_vars, shape_invariants),
        func_graph=util.WhileBodyFuncGraph(body_name),
        add_control_dependencies=add_control_dependencies)
    # Add external captures of body to the list of loop vars.
    # Note that external tensors will be treated as loop invariants, i.e.,
    # the value of that tensor in each iteration is the same as it was at the
    # beginning of the loop execution.
    loop_vars = loop_vars + body_graph.external_captures
    # TODO(srbs): Update lowering code to create _Enter nodes with
    # is_constant=True for inputs that are directly passed to outputs.
    body_graph.outputs.extend(body_graph.internal_captures)

    # Capture `external_captures` of `body_graph` in `cond_graph` so that it
    # expects to receive those as arguments.
    # TODO(b/118457764): Dedup tensors that are captured in both the cond and
    # body. This logic already exists in cond_v2.
    with cond_graph.as_default():
      for external_capture in body_graph.external_captures:
        assert external_capture not in cond_graph.captures, (
            "Looks like both cond and body are capturing the same tensor %s. "
            "This is not supported yet. For now consider passing,"
            " this as a loop variable." % str(external_capture))
        cond_graph.capture(external_capture)

    # Export all tensors in the loop body that may be needed for gradient
    # computation. We do this by accumulating the intermediate values in
    # TensorLists.
    intermediate_tensors = _get_intermediates(body_graph)

    for intermediate_tensor in intermediate_tensors:
      # TODO(srbs): Cache and re-use empty tensor lists.
      tensor_list = list_ops.empty_tensor_list(
          element_dtype=intermediate_tensor.dtype,
          element_shape=_get_tensor_convertible_shape(
              intermediate_tensor.shape))
      loop_vars.append(tensor_list)
      with cond_graph.as_default():
        # Add a placeholder to cond_graph's inputs corresponding to the
        # tensor_list.
        cond_graph.capture(tensor_list)
      with body_graph.as_default():
        # Push the intermediate tensor to the tensor list. This captures the
        # `tensor_list` as well.
        appended_tensor_list = list_ops.tensor_list_push_back(
            tensor_list,
            intermediate_tensor)
        # Add this modified tensor list to the list of outputs.
        body_graph.outputs.append(appended_tensor_list)

    # Make sure that the shapes of the loop outputs are compatible with the
    # shape invariants, or the shapes of the loop vars if the invariants are not
    # specified.
    num_flattened_outputs = len(nest.flatten(orig_loop_vars))
    _check_shapes_compat(
        body_graph.outputs[1:1 + num_flattened_outputs],
        nest.flatten(shape_invariants[1:1 + len_orig_loop_vars]),
        nest.flatten(loop_vars[1:1 + len_orig_loop_vars]))
    flattened_loop_vars = nest.flatten(loop_vars)
    _check_num_inputs_outputs(cond_graph, body_graph,
                              len(flattened_loop_vars))

    outputs = gen_functional_ops._while(
        flattened_loop_vars,
        util.create_new_tf_function(cond_graph),
        util.create_new_tf_function(body_graph),
        output_shapes=[t.shape for t in body_graph.outputs],
        name=scope)

    _copy_handle_data(body_graph.outputs, outputs)
    _maybe_set_lowering_attr(outputs[0].op)

    # Return identities for each output of the While op, rather than the output
    # of the While op directly. This makes pruning work if the output of
    # while_loop() is fetched: the lowering pass converts the While outputs into
    # IdentityN outputs, which if fetched will cause all ops in the body to be
    # run (since it takes all exit ops as input). After lowering, each output
    # identity op will end up with only the appropriate exit op as input.
    outputs = tuple(array_ops.identity(t) for t in outputs)

  # First var is loop counter.
  if num_flattened_outputs == 1:
    return outputs[1]
  else:
    return _pack_sequence_as(orig_loop_vars,
                             outputs[1:1 + num_flattened_outputs])


@ops.RegisterGradient("While")
def _WhileGrad(op, *grads):  # pylint: disable=invalid-name
  """The gradient of a While op produced by while_loop."""
  body_graph = _get_body_graph(op)

  # Set the incoming gradient of TensorArray handles to None. The gradient
  # implementation currently assumes all resource tensors correspond to float32
  # ResourceVariables, which can lead to runtime shape errors when used with a
  # TensorArray. This is a workaround until TensorArrays are reimplemented with
  # TensorLists instead of resources.
  grads = [
      None if _is_tensor_array_handle(output) else grad
      for grad, output in zip(grads, op.outputs)
  ]

  # Ensure that all non-resource trainable outputs have incoming gradients.
  assert all(g is not None or o.dtype == dtypes.resource or
             not gradients_impl.IsTrainable(o)
             for o, g in zip(op.outputs, grads)
            ), "All trainable loop vars must receive incoming gradients."
  # We compute the gradient for the sub-graph between trainable ys and xs
  # with non-None incoming gradients. We later pad the None's to the list of
  # outputs.
  ys, xs, non_none_grads = zip(*[(y, x, grad) for (y, x, grad) in zip(
      body_graph.outputs, body_graph.inputs, grads) if grad is not None])

  body_grad_graph, args = _create_grad_func(
      ys, xs, non_none_grads, body_graph,
      util.unique_grad_fn_name(body_graph.name), op)

  intermediate_tensors = _get_intermediates(body_grad_graph)

  for intermediate_tensor in intermediate_tensors:
    tensor_list = list_ops.empty_tensor_list(
        element_dtype=intermediate_tensor.dtype,
        element_shape=_get_tensor_convertible_shape(intermediate_tensor.shape))
    with body_grad_graph.as_default():
      tensor_list_ph = body_grad_graph.capture(tensor_list, whitelisted=True)
      # Push the intermediate tensor to the tensor list.
      appended_tensor_list = list_ops.tensor_list_push_back(tensor_list_ph,
                                                            intermediate_tensor)
      # Add this modified tensor list to the list of outputs.
      body_grad_graph.outputs.append(appended_tensor_list)

  def grad_cond(counter, max_iters, *unused_args):
    return counter < max_iters

  loop_vars = args + body_grad_graph.external_captures
  grad_cond_name = util.unique_grad_fn_name(op.get_attr("cond").name)
  cond_grad_graph = func_graph_module.func_graph_from_py_func(
      grad_cond_name, grad_cond, loop_vars, {},
      func_graph=util.WhileCondFuncGraph(grad_cond_name))

  _check_num_inputs_outputs(cond_grad_graph, body_grad_graph, len(loop_vars))

  outputs = gen_functional_ops._while(
      loop_vars,
      util.create_new_tf_function(cond_grad_graph),
      util.create_new_tf_function(body_grad_graph),
      output_shapes=[t.shape for t in body_grad_graph.outputs],
      name="%s_grad" % op.name)

  _copy_handle_data(body_grad_graph.outputs, outputs)
  _maybe_set_lowering_attr(outputs[0].op)

  # Set None as the output gradient for tensors with None input gradient
  # e.g. TensorArray handles.
  # outputs[0] is the loop counter.
  # outputs[1] is the total number of loop iterations.
  index = 2
  none_padded_outputs = []
  for g in grads:
    if g is None:
      none_padded_outputs.append(None)
    else:
      none_padded_outputs.append(outputs[index])
      index += 1
  return none_padded_outputs


# TODO(srbs): Pull this into common utils for cond_v2 and while_v2.
def _get_body_graph(while_op):
  """Returns `FuncGraph` for the while body.

  Args:
    while_op: The While Operation.

  Returns:
    `FuncGraph` for the while body.
  """
  # TODO(srbs): Handle TensorShapeProto in function_def_to_graph.input_shapes.
  input_shapes = [
      tensor_shape.TensorShape(s) for s in while_op.get_attr("output_shapes")
  ]
  func_name = while_op.get_attr("body").name
  fdef = while_op.graph._get_function(func_name).definition
  # `while_op.graph` may not be the same as `ops.get_default_graph()` e.g.
  # if the `while_op` is in the body of another if/while/defun. We build the
  # `func_graph` with `while_op.graph` as its `outer_graph`. This resembles how
  # the `FuncGraph` was built in the forward pass. We need this so that we can
  # appropriately capture references to outer tensors in the nested grad graphs.
  with while_op.graph.as_default():
    func_graph = function_def_to_graph.function_def_to_graph(fdef, input_shapes)
  func_graph._while = while_op
  return func_graph


def _create_grad_func(ys, xs, grads, func_graph, name, while_op):
  """Builds and returns the gradient FuncGraph of `func_graph` and its args.

  The returned grad_func_graph must be called with the returned
  args + grad_func_graph.captures.

  Args:
    ys: A `Tensor` or list of tensors to be differentiated.
    xs: A `Tensor` or list of tensors to be used for differentiation.
    grads: The incoming grads for `ys`.
    func_graph: FuncGraph for the forward body function.
    name: Name of the returned gradient function.
    while_op: The forward While op.

  Returns:
    2-tuple of (grad_func_graph, args).
  """
  assert len(ys) == len(grads)

  counter = constant_op.constant(0.)
  total_iters = while_op.outputs[0]

  args = [counter, total_iters] + list(grads)
  # Note: The returned function does not have `args` in the list of
  # `external_captures`.
  grad_func_graph = func_graph_module.func_graph_from_py_func(
      name,
      lambda *args: _grad_fn(ys, xs, args, func_graph),
      args, {},
      func_graph=_WhileBodyGradFuncGraph(name, func_graph))

  # Add the popped accumulators to the list of outputs.
  for internal_capture in grad_func_graph.internal_captures:
    if internal_capture in grad_func_graph.popped_tensor_lists:
      grad_func_graph.outputs.append(
          grad_func_graph.popped_tensor_lists[internal_capture])
    elif internal_capture.dtype == dtypes.resource:
      grad_func_graph.outputs.append(internal_capture)
    else:
      raise ValueError("Tensor %s is in list of internal_captures but is"
                       " neither a resource nor is in popped_tensor_lists." %
                       str(internal_capture))

  return grad_func_graph, args


def _grad_fn(ys, xs, args, func_graph):
  """Computes the gradient of `func_graph` in the current graph.

  This function builds the gradient graph of the corresponding forward-pass
  `func_graph` by differentiating `func_graph`'s outputs w.r.t. its inputs.

  Args:
    ys: A `Tensor` or list of tensors to be differentiated.
    xs: A `Tensor` or list of tensors to be used for differentiation.
    args: The input arguments.
      args[0] - Loop counter
      args[1] - Total number of iterations.
      args[2:] - Incoming gradients for `func_graph.outputs`.
    func_graph: function.FuncGraph. The corresponding forward-pass function.

  Returns:
    The output gradient Tensors.
  """
  grad_ys = args[2:]

  # Build the gradient graph. Note that this builds the gradient computation of
  # func_graph in the current graph, which requires capturing tensors from
  # func_graph. The captured func_graph tensors are resolved to external tensors
  # in _resolve_grad_inputs.
  # TODO(srbs): Mark GradientsHelper as public?
  grad_outs = gradients_impl._GradientsHelper(
      ys, xs, grad_ys=grad_ys, src_graph=func_graph)

  assert all([g is not None for g in grad_outs])
  counter = args[0]
  total_iters = args[1]
  return [counter + 1, total_iters] + grad_outs


def _get_intermediates(func_graph):
  """Returns all tensors in `func_graph` that should be accumulated."""
  # We currently accumulate output tensors of most ops in the function and rely
  # on the pruning pass to get rid of the unused accumulators at runtime.
  # However, this can bloat the GraphDef and make debugging harder so we perform
  # some optimizations.
  #
  # Optimization we currently perform:
  # 1. We do not accumulate tensors which already have an accumulator
  #    in the loop body.
  # 2. We do not accumulate outputs of Identity nodes. When building the
  #    FuncGraph, we add an Identity node for each output (see
  #    `AutomaticControlDependencies.mark_as_return`). Accumulating outputs
  #    of all these nodes bloats the GraphDef quite a bit so we remove those.
  #    Since the gradient of an Identity node does not rely on its forward op's
  #    input this is safe to do.
  #
  # Other possible optimizations:
  # 1. Only accumulate tensors that will be required by the backward pass.
  #    This will require running the gradient pass and hence would increase the
  #    graph building time for the forward pass.
  # 2. Do not accumulate Const nodes created inside the loop body.
  # 3. Do not accumulate inputs that are passed as-is, e.g. loop invariants.
  # TODO(srbs): 2 and 3 may be hard optimizations for the runtime optimizer
  # since it requires knowledge of the while loop semantics. If so, consider
  # doing those here.
  intermediates = []

  for op in func_graph.get_operations():
    if op.type == "Identity":
      continue
    for o in op.outputs:
      if (o != func_graph.inputs[0] and  # Loop counter.
          o.dtype != dtypes.resource and  # Do not accumulate resource tensors.
          _get_accumulator(o) is None):  # Has existing accumulator.
        intermediates.append(o)
  return intermediates


def _get_accumulator(tensor):
  r"""Returns TensorList if any containing accumulated values of tensor.

  We try to find a pattern of the form:

     input_tl   tensor
        \        /
    (TensorListPushBack)
            |
        output_tl

  which satisfies the following conditions:

  1. input_tl must be in tensor.graph.inputs.
  2. output_tl or Identity(output_tl) must be in tensor.graph.outputs.
  3. tensor.graph.input_index(input_tl) == tensor.graph.output_index(output_t).

  output_tl or Identity(output_tl) (whichever is in tensor.graph.outputs) is
  returned if such a pattern is found else None is returned.

  Args:
    tensor: The Tensor to be accumulated.

  Returns:
    A variant tensor in the same graph as `tensor` or None if no accumulator is
    found.
  """
  assert isinstance(tensor.graph, func_graph_module.FuncGraph)

  def get_func_graph_output(t):
    """Returns t or Identity(t) whichever exists in graph outputs else None."""
    if t in tensor.graph.outputs:
      return t
    # tf.defun adds an Identity for each output, check whether that is the case.
    identity_op = t.consumers()[0]
    if (identity_op.type == "Identity" and
        identity_op.outputs[0] in tensor.graph.outputs):
      return identity_op.outputs[0]
    return None

  for consumer in tensor.consumers():
    # Find the consumer that is a TensorListPushBack node whose TensorList input
    # is in the list of function inputs.
    if (consumer.type != "TensorListPushBack" or
        consumer.inputs[0] not in tensor.graph.inputs):
      continue

    output = get_func_graph_output(consumer.outputs[0])
    if output is None:
      # The TensorList output of `consumer` is not in the list of function
      # outputs.
      continue

    accum_input_idx = tensor.graph.inputs.index(consumer.inputs[0])
    accum_output_idx = tensor.graph.outputs.index(output)
    if accum_input_idx == accum_output_idx:
      return output
  return None


class _WhileBodyGradFuncGraph(util.WhileBodyFuncGraph):
  """FuncGraph for the gradient function of the body of a While op.

  Contains the logic for capturing the tensors from the body of the forward
  While op which is as follows:
  1. If the tensor is of resource type (these are not accumulated):
     a. Ensure that the tensor is a loop invariant, i.e., it exists in both loop
        inputs and outputs at the same index.
     b. Lookup the corresponding resource tensor in the forward outer graph and
        try to capture that.
  2. If the tensor is not of resource type:
     a. Find the accumulator for that tensor.
     b. Capture the forward While op output tensor corresponding to the
        accumulator in this FuncGraph.
     c. Pop a value from the captured placeholder and use it as the captured
        value for the forward pass tensor.

  This only allows capturing tensors in the forward graph. A ValueError is
  raised if an attempt is made to capture a tensor not in the forward graph.
  To manually capture capture a tensor that is not in the forward graph, call
  `capture` with `whitelisted=True`.

  Note: The `captures` dict does not contain the forward tensor since it is not
  directly captured. It contains the accumulator corresponding to this forward
  tensor.

  Attributes:
    popped_tensor_lists: Dict from the captured accumulator placeholder to the
      TensorList obtained after popping the intermediate tensor from it. The
      values of this dict need to be added to the list of outputs.
  """

  def __init__(self, name, forward_graph):
    super(_WhileBodyGradFuncGraph, self).__init__(name)
    self.popped_tensor_lists = {}
    # FuncGraph for the body of the forward While op.
    self._forward_graph = forward_graph
    # Dict from forward intermediate tensor to its indirectly captured tensor
    # in this graph. Indirect capturing happens in two ways:
    # 1. For non-resource tensors we capture their accumulators from the forward
    #    outer graph and pop values from that accumulator inside this graph
    #    using TensorListPopBack.
    # 2. For resource tensors we directly capture their corresponding tensor
    #    in the forward outer graph.
    self._indirect_captures = {}
    # Dict from forward graph tensor to its corresponding tensor in
    # `forward_graph.outer_graph`. For a non-resource tensor the value is the
    # forward While op's "output" corresponding its accumulator. For a resource
    # tensor it is the While op's "input" for the resource. Note: We disallow
    # creation of resources inside the while loop so if a resource tensor exists
    # inside while loop it must be a loop input.
    self._inner_to_outer_tensor = {}

  def capture(self, tensor, name=None, whitelisted=False):
    """Selectively captures external tensors.

    If `whitelisted` is False only allows capturing tensors in the
    `_forward_graph`.

    Args:
      tensor: Tensor. May be from this FuncGraph or a different graph.
      name: Optional name if a placeholder is created.
      whitelisted: If False (default), only allows capturing tensors from the
        forward graph.

    Returns:
      The placeholder in this graph for the tensor.

    Raises:
      ValueError: If attempting to capture an external tensor not in the forward
        graph with `whitelisted` set to False.
    """
    if (not whitelisted and tensor.graph is not self and
        tensor.graph != self._forward_graph):
      raise ValueError("Attempting to capture tensor", str(tensor),
                       " which is not in the forward graph but in ",
                       _graph_name(tensor.graph), ".")
    return super(_WhileBodyGradFuncGraph, self).capture(tensor, name)

  def _capture_helper(self, tensor, name):
    if tensor.graph is not self._forward_graph:
      return super(_WhileBodyGradFuncGraph, self)._capture_helper(tensor, name)

    while tensor.op.type == "Identity":
      # We do not accumulate the output of identity nodes so we try to capture
      # the input of the Identity node instead.
      tensor = tensor.op.inputs[0]

    captured_tensor = self._indirect_captures.get(tensor)
    if captured_tensor is not None:
      # For GradientTape housekeeping.
      assert self._inner_to_outer_tensor[tensor] in self.captures
      super(_WhileBodyGradFuncGraph, self)._capture_helper(
          self._inner_to_outer_tensor[tensor], name)
      return captured_tensor

    if tensor.dtype == dtypes.resource:
      # Resource-type tensors are not accumulated.
      # If a resource tensor exists in the loop body it must either be a loop
      # input or an output of a nested While op inside the loop body which
      # had captured the external resource.
      if tensor in self._forward_graph.inputs:
        index = self._forward_graph.inputs.index(tensor)
      elif tensor.op.type == "While":
        # Captured resources occur at the same index in the lists of inputs and
        # outputs of a while op. So we lookup the input of `tensor.op` at the
        # same index as the index of `tensor` in the `tensor.op.outputs`.
        index = self._forward_graph.inputs.index(
            tensor.op.inputs[tensor.value_index])
      else:
        raise ValueError(
            "Taking gradient of a while loop which creates"
            " a resource in its body is not supported: %s" % str(tensor))
      # This must be a loop invariant.
      assert self._forward_graph.inputs[index] == self._forward_graph.outputs[
          index], "Resource tensors must be loop invariants %s." % str(
              self._forward_graph._while.inputs[index])
      tensor_in_outer_graph = self._forward_graph._while.inputs[index]
      self._inner_to_outer_tensor[tensor] = tensor_in_outer_graph
      self._indirect_captures[tensor] = self.capture(
          tensor_in_outer_graph, whitelisted=True)
      return self._indirect_captures[tensor]

    assert tensor not in self._inner_to_outer_tensor

    accumulator = None

    # Find the TensorList that was used to accumulate the tensors of this
    # intermediate tensor.
    accumulator = _get_accumulator(tensor)
    if accumulator is None:
      raise ValueError("Reference to un-accumulated intermediate tensor: ",
                       tensor.name)
    assert accumulator.graph == self._forward_graph
    # Get the While op output corresponding to the accumulator.
    accumulator = self._forward_graph._while.outputs[self._forward_graph.outputs
                                                     .index(accumulator)]

    assert accumulator.graph == self._forward_graph.outer_graph
    self._inner_to_outer_tensor[tensor] = accumulator

    # Capture the `accumulator`.
    accumulator_ph = super(_WhileBodyGradFuncGraph, self)._capture_helper(
        accumulator, name)
    new_tensor_list, captured_tensor = list_ops.tensor_list_pop_back(
        accumulator_ph, element_dtype=tensor.dtype)
    self._indirect_captures[tensor] = captured_tensor
    self.popped_tensor_lists[accumulator_ph] = new_tensor_list
    return captured_tensor


def _check_shapes_compat(output_tensors, shape_invariants, input_tensors):
  for (t, shape, input_t) in zip(output_tensors, shape_invariants,
                                 input_tensors):
    if not control_flow_ops._ShapeLessThanOrEqual(t.shape, shape):
      raise ValueError(
          "Input tensor '%s' enters the loop with shape %s, but has "
          "shape %s after one iteration. To allow the shape to vary across "
          "iterations, use the `shape_invariants` argument of tf.while_loop to "
          "specify a less-specific shape." % (input_t.name, shape, t.shape))


def _check_num_inputs_outputs(cond_graph, body_graph, num_flattened_loop_vars):
  """Checks the number of inputs/outputs of `cond_graph` and `body_graph`."""
  assert len(cond_graph.inputs) == num_flattened_loop_vars, (
      "cond_graph takes %d inputs; Expected: %d" % (len(cond_graph.inputs),
                                                    num_flattened_loop_vars))
  assert len(cond_graph.outputs) == 1, (
      "cond_graph has %d outputs; Expected: 1" % len(cond_graph.outputs))
  assert len(body_graph.inputs) == num_flattened_loop_vars, (
      "body_graph takes %d inputs; Expected: %d" % (len(cond_graph.inputs),
                                                    num_flattened_loop_vars))
  assert len(body_graph.outputs) == num_flattened_loop_vars, (
      "body_graph has %d outputs; Expected: %d" % (len(body_graph.outputs),
                                                   num_flattened_loop_vars))


def _copy_handle_data(src_tensors, tgt_tensors):
  for src_t, tgt_t in zip(src_tensors, tgt_tensors):
    custom_gradient.copy_handle_data(src_t, tgt_t)


# TODO(srbs): Move to common utils for cond_v2 and while_v2.
def _maybe_set_lowering_attr(op):
  """Sets the flag to enable lowering on the `While` op if necessary.

  Lowering allows while_v2 to avoid some of the limitations of Functions,
  allowing users to specify devices & colocation inside of while_v2
  branches, and enabling non-strict evaluation & partial pruning of while_v2
  branches. This brings while_v2 closer to feature parity with
  tf.while_loop.

  However, we do not lower `While` in the XLA context because it is easier
  for XLA to apply its own optimizations when dealing with un-lowered
  `While` operators than with low-level control flow primitives.

  Args:
    op: The While op.
  """
  if not control_flow_util.IsInXLAContext(op):
    # pylint: disable=protected-access
    op._set_attr("_lower_using_switch_merge", attr_value_pb2.AttrValue(b=True))
    # pylint: enable=protected-access


def _get_tensor_convertible_shape(shape):
  assert isinstance(shape, tensor_shape.TensorShape)
  if shape.is_fully_defined():
    return shape
  if not shape:  # Unknown shape.
    return -1
  # Partially defined shape.
  shape_list = shape.as_list()
  shape_list = [s if s is not None else -1 for s in shape_list]
  return ops.convert_to_tensor(shape_list)


def _graph_name(graph):
  if isinstance(graph, func_graph_module.FuncGraph):
    return graph.name
  return "Base"


def _is_tensor_array_handle(tensor):
  """Returns whether tensor is a TensorArray handle."""
  if tensor.dtype != dtypes.resource:
    return False

  if tensor.op.type == "While":
    # We assume that any resource outputs of a While op correspond to a captured
    # resource input (as opposed to a loop variable specified by the user).
    # NOTE(skyewm): we could actually check this, but I can't think of when you
    # would have a resource loop variable.
    tensor = tensor.op.inputs[tensor.value_index]

  # TODO(b/118452219): add test coverage for this.
  tensor = func_graph_module.maybe_captured(tensor)

  return tensor.op.type in TENSOR_ARRAY_HANDLE_OPS


def _pack_sequence_as(structure_with_tas, loop_vars):
  """Like `nest.pack_sequence_as` but also replaces flows with TensorArrays."""

  def flow_to_tensor_array(flow, ta):  # pylint: disable=missing-docstring
    if isinstance(ta, tensor_array_ops.TensorArray):
      # pylint: disable=protected-access
      new_ta = tensor_array_ops.TensorArray(
          dtype=ta.dtype,
          handle=ta.handle,
          flow=flow,
          infer_shape=ta._infer_shape,
          colocate_with_first_write_call=ta._colocate_with_first_write_call)
      new_ta._colocate_with = ta._colocate_with
      new_ta._element_shape = ta._element_shape
      # pylint: enable=protected-access
      return new_ta
    return flow

  flattened_loop_vars = [
      flow_to_tensor_array(*z)
      for z in zip(nest.flatten(loop_vars), nest.flatten(structure_with_tas))
  ]
  return nest.pack_sequence_as(structure_with_tas, flattened_loop_vars)


def _tensor_array_to_flow(loop_vars):

  def f(maybe_ta):
    if isinstance(maybe_ta, tensor_array_ops.TensorArray):
      return maybe_ta.flow
    return maybe_ta

  return nest.map_structure(f, loop_vars)


def _build_signature(loop_vars, shape_invariants):
  return nest.pack_sequence_as(loop_vars, [
      tensor_spec.TensorSpec(s, t.dtype, name=t.op.name)
      for s, t in zip(nest.flatten(shape_invariants), nest.flatten(loop_vars))
  ])


# pylint: enable=protected-access
