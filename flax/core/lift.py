# Copyright 2023 The Flax Authors.
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

"""Jax transform lifting."""

import collections
import dataclasses
import functools
from typing import (Any, Callable, Dict, Generic, Iterable, List, Mapping,
                    Optional, Sequence, Tuple, TypeVar, Union)
import warnings


from . import axes_scan
from . import meta
from flax import traceback_util, traverse_util
from .frozen_dict import freeze
from .frozen_dict import unfreeze
import jax
import jax.numpy as jnp
from jax import random
from .scope import (CollectionFilter, DenyList, PRNGSequenceFilter,  # pylint: disable=g-multiple-import
                    Filter, Scope, group_collections, in_filter,
                    intersect_filters, is_filter_empty, subtract_filters,
                    union_filters)

traceback_util.register_exclusion(__file__)

T = TypeVar('T')


def tree_map_rngs(fn, tree):
  """Needed for mapping JAX random.* functions over KeyArray leaves."""
  return jax.tree_util.tree_map(
      fn, tree, is_leaf=lambda x: isinstance(x, random.KeyArray))


def _dedup_scopes(scopes):
  """Deduplicated scopes."""
  paths = []
  # must preseve insertion order for duplication to work correctly
  minimal_set = collections.OrderedDict((s, ()) for s in scopes)
  for leaf in scopes:
    scope = leaf.parent
    max_parent = leaf
    max_parent_path = ()
    path = [leaf.name]
    while scope is not None:
      if scope in minimal_set:
        max_parent = scope
        max_parent_path = tuple(reversed(path))
      path.append(scope.name)
      scope = scope.parent
    if max_parent is not leaf and leaf in minimal_set:
      del minimal_set[leaf]
    paths.append((max_parent, max_parent_path))
  return tuple(minimal_set), tuple(paths)


def _dup_scopes(orig_scopes, scopes, paths):
  """Duplicated scopes."""
  mapping = dict(zip(orig_scopes, scopes))
  scopes = []
  for root, path in paths:
    scope = mapping[root]
    for name in path:
      scope = scope.push(name, reuse=True)
    scopes.append(scope)
  return scopes


def _transpose(xs):
  return tuple(zip(*xs))


def pack(fn: Callable[..., Any],
         in_variable_filters: Sequence[CollectionFilter],
         out_variable_filters: Sequence[CollectionFilter],
         rng_filters: Sequence[PRNGSequenceFilter],
         name=None,
         enable_kwargs=False) -> Callable[..., Any]:
  """Pack variables and rngs for functional transformations.

  The pack function is the building block for all other lifted transformations.

  Args:
    fn: The function to pack. `fn` has the signature
      `(scope_fn, repack_fn, variable_groups, rng_groups, *args) ->
      (output, packed_variables)`.
    in_variable_filters: Input variable filters.
    out_variable_filters: Output variable filters.
    rng_filters: RNG filters.
    name: The name of the packed scope.
    enable_kwargs: Whether to enable kwargs or not.
  Returns:
    A callable which expects a scope as the first argument.
  """
  @functools.wraps(fn)
  def wrapper(scope_tree: Scope, *args, **kwargs):
    if not enable_kwargs and kwargs:
      msg = 'kwargs are not supported in {}, so \"{}\" is(are) ignored'
      warnings.warn(msg.format(name, ', '.join(kwargs.keys())), RuntimeWarning)
    # pylint: disable=protected-access
    scopes, treedef = jax.tree_util.tree_flatten(scope_tree)
    scopes, paths = _dedup_scopes(scopes)

    variable_groups_xs = []

    for scope in scopes:
      scope._validate_trace_level()
      scope._populate_collections()
      variable_groups_xs.append(group_collections(
          scope._variables, in_variable_filters))
    variable_groups_xs_t = _transpose(variable_groups_xs)

    # Make sure that in-only variable collections are frozen
    for variable_group_xs in variable_groups_xs_t:
      for variable_group in variable_group_xs:
        for col_name, collection in variable_group.items():
          col_in_out = any(
              in_filter(col_filter, col_name)
              for col_filter in out_variable_filters)
          if not col_in_out:
            variable_group[col_name] = freeze(collection)
    rng_groups_xs = []
    inner_rng_counters = []
    for scope in scopes:
      rng_counters = scope.rng_counters
      rng_groups = group_collections(scope.rngs, rng_filters)
      rng_groups_xs.append(rng_groups)
      inner_rng_counters.append(rng_counters)
    rng_groups_xs_t = _transpose(rng_groups_xs)

    inner_scopes: List[Scope] = []

    def scope_fn(variable_groups_xs_t,
                 rng_groups_xs_t,
                 mutable_filter: CollectionFilter = True):
      nonlocal inner_scopes
      for inner_scope in inner_scopes:
        inner_scope.invalidate()
      inner_scopes = []
      mutable: Filter = False
      for out_filter in out_variable_filters:
        mutable = union_filters(mutable, out_filter)
      # could be () in the edge case where no rngs or variable_groups are lifted
      # in this case fallback to ((),) * len(scopes) to make sure the zip has
      # something to iterate over for each scope.
      variable_groups_xs = _transpose(variable_groups_xs_t) or (
          (),) * len(scopes)
      rng_groups_xs = _transpose(rng_groups_xs_t) or ((),) * len(scopes)
      assert len(variable_groups_xs) == len(scopes)
      assert len(rng_groups_xs) == len(scopes)
      for variable_groups, rng_groups, scope, rng_counters in zip(
          variable_groups_xs, rng_groups_xs, scopes, inner_rng_counters):
        variables = {}
        rngs = {}
        for variable_group in variable_groups:
          variables.update(variable_group)
        for rng_group in rng_groups:
          rngs.update(rng_group)
        # make sure variable dicts are cloned and can't be manipulated by ref
        # sharing.
        variables = jax.tree_util.tree_map(lambda x: x, variables)
        scope_mutable = intersect_filters(
            intersect_filters(scope.mutable, mutable), mutable_filter)
        new_path = scope.path
        if name:
          if new_path:
            new_path = new_path[:-1] + (f'{name}({new_path[-1]})',)
          else:
            new_path = (f'{name}()',)
        inner_scope = Scope(
            variables, name=scope.name, rngs=rngs,
            mutable=scope_mutable, parent=None,
            path=new_path, flags=scope.flags)
        inner_scope.rng_counters = rng_counters
        inner_scopes.append(inner_scope)
      inner_scopes = _dup_scopes(scopes, inner_scopes, paths)
      return treedef.unflatten(inner_scopes)

    def repack(inner_scope_tree):
      inner_scopes = treedef.flatten_up_to(inner_scope_tree)
      inner_scopes, inner_paths = _dedup_scopes(inner_scopes)
      inner_scopes = list(inner_scopes)
      assert [p for _, p in paths] == [p for _, p in inner_paths]
      out_variable_groups_xs = []
      for inner_scope in inner_scopes:
        inner_scope.invalidate()
        inner_scope._validate_trace_level()
        mutable_variables = {key: val for key, val
                             in inner_scope._variables.items()
                             if in_filter(inner_scope.mutable, key)}
        out_variable_groups = group_collections(
            mutable_variables, tuple(out_variable_filters) + (True,))
        remainder = tuple(out_variable_groups[-1].keys())
        if remainder:
          raise ValueError(f'unmapped output variables: {remainder}')
        out_variable_groups_xs.append(out_variable_groups[:-1])

      return _transpose(out_variable_groups_xs)

    try:
      if enable_kwargs:
        y, out_variable_groups_xs_t = fn(
            scope_fn, repack,
            variable_groups_xs_t, rng_groups_xs_t,
            *args, **kwargs)
      else:
        y, out_variable_groups_xs_t = fn(
            scope_fn, repack,
            variable_groups_xs_t, rng_groups_xs_t,
            *args)
    finally:
      for inner_scope in inner_scopes:
        inner_scope.invalidate()
    out_variable_groups_xs = _transpose(out_variable_groups_xs_t)
    for scope, out_variable_groups, rng_counters in zip(scopes,
                                                        out_variable_groups_xs,
                                                        inner_rng_counters):
      for out_variable_group in out_variable_groups:
        for col_name, collection in out_variable_group.items():
          if not scope.is_mutable_collection(col_name):
            # Some lifted transforms like scan return redundant variables.
            continue
          for var_name, value in collection.items():
            scope.put_variable(col_name, var_name, value)
    return y
  return wrapper


id_fn = lambda x: x


def map_variables(fn: Callable[..., Any],
                  mapped_collections: CollectionFilter,
                  map_in_fn: Callable[..., Any] = id_fn,
                  map_out_fn: Callable[..., Any] = id_fn,
                  init: bool = False,
                  mutable: bool = False,
                  rngs: PRNGSequenceFilter = True,
                  variables: CollectionFilter = True) -> Callable[..., Any]:
  """Map Variables inside a scope.

  Args:
    fn: the function to be transformed.
    mapped_collections: the collection(s) to be transformed.
    map_in_fn: creates a view of the target variables.
    map_out_fn: transforms the updated variables in the view after mutation.
    init: If True, variables are initialized before transformation.
    mutable: If True, the mapped variable collections will be mutable.
    rngs: PRNGSequences added to the transformed scope (default: all).
    variables: Additional Variable collections added to the transformed scope.
      Besides those specified by `target` (default: all).

  Returns:
    A callable expecting a scope as the first argument.
  """
  is_target_out = mutable or init

  def wrapper(scope_fn, repack, variable_groups, rng_groups, *args, **kwargs):
    target, variables = variable_groups
    if init:
      scopes = scope_fn((target, variables), rng_groups)
      has_mutable_cols = any(not is_filter_empty(scope.mutable)
                             for scope in jax.tree_util.tree_leaves(scopes))
      if has_mutable_cols:
        fn(scopes, *args, **kwargs)
        target, _ = repack(scopes)
        target = tuple(map_out_fn(x) for x in target)
    target = tuple(map_in_fn(unfreeze(x)) for x in target)
    mfilter = True
    if not is_target_out:
      # mapped collections should not be mutable
      # unless the mapping supports it (by init=True or mutable=True)
      mfilter = subtract_filters(mfilter, mapped_collections)
    scopes = scope_fn((target, variables), rng_groups, mutable_filter=mfilter)
    y = fn(scopes, *args, **kwargs)
    out_target, out_vars = repack(scopes)
    if is_target_out:
      out_target = tuple(map_out_fn(x) for x in out_target)
    return y, (out_target, out_vars)

  in_vars = (mapped_collections, variables)
  out_vars = in_vars if is_target_out else (False,
                                            subtract_filters(
                                                variables, mapped_collections))
  return pack(
      wrapper,
      in_vars,
      out_vars, (rngs,),
      enable_kwargs=True,
      name='map_variables')


def swap_collection(fn: Callable[..., Any], col_a: str, col_b: str):
  """Swap two collections."""
  def swap(target):
    a = target[col_a] if col_a in target else {}
    b = target[col_b] if col_b in target else {}
    target[col_b], target[col_a] = a, b
    return target

  return map_variables(fn, (col_a, col_b), swap, swap, mutable=True)


@dataclasses.dataclass(frozen=True)
class In(Generic[T]):
  """Specifies a variable collection should only be lifted as input."""
  axis: T


@dataclasses.dataclass(frozen=True)
class Out(Generic[T]):
  """Specifies a variable collection should only be lifted as output."""
  axis: T


def _split_in_out_axes(xs: Mapping[CollectionFilter, Any]):
  unpack = lambda v: v.axis if isinstance(v, (In, Out)) else v
  in_axes = {k: unpack(v) for k, v in xs.items() if not isinstance(v, Out)}
  out_axes = {k: unpack(v) for k, v in xs.items() if not isinstance(v, In)}
  return in_axes, out_axes


Axis = Optional[int]
InOutAxis = Union[Axis, In[Axis], Out[Axis]]


def _bwd_wrapper(treedef, bwd_fn, tangent):
  vars_grad, *inputs_grad = bwd_fn(tangent)
  vars_grad = treedef.unflatten(vars_grad)
  return (vars_grad, *inputs_grad)


def vjp(
    fn: Callable[..., Any],
    scope: Scope,
    *primals,
    has_aux: bool = False,
    reduce_axes=(),
    vjp_variables: CollectionFilter = 'params',
    variables: CollectionFilter = True,
    rngs: PRNGSequenceFilter = True,
) -> Union[Tuple[Any, Callable[..., Any]], Tuple[Any, Callable[..., Any], Any]]:
  """A lifted version of ``jax.vjp``.

  See ``jax.vjp`` for the unlifted vector-Jacobiam product (backward gradient).

  Note that a gradient is returned for all variables in the collections
  specified by `vjp_variables`. However, the backward funtion only expects
  a cotangent for the return value of `fn`. If variables require a co-tangent
  as well they can be returned from `fn` using `scope.variables()`.

  Example::

    def learn_scale(scope, x, y):
      p = scope.param('scale', nn.initializers.zeros_init(), ())
      return p * x * y
    def f(scope, x, y):
      z, bwd = lift.vjp(learn_scale, scope, x, y)
      params_grad, x_grad, y_grad = bwd(jnp.ones(z.shape))
      return z, params_grad, x_grad, y_grad

  Args:
    fn: Function to be differentiated. Its arguments should be arrays, scalars,
      or standard Python containers of arrays or scalars. It should return an
      array, scalar, or standard Python container of arrays or scalars. It will
      receive the scope and primals as arguments.
    scope: The scope of which the variables will be differentiated.
    *primals: A sequence of primal values at which the Jacobian of ``fn``
      should be evaluated. The length of ``primals`` should be equal to the
      number of positional parameters to ``fn``. Each primal value should be a
      tuple of arrays, scalar, or standard Python containers thereof.
    has_aux: Optional, bool. Indicates whether ``fn`` returns a pair where the
     first element is considered the output of the mathematical function to be
     differentiated and the second element is auxiliary data. Default False.
    reduce_axes: Optional, tuple of axis names. If an axis is listed here, and
      ``fn`` implicitly broadcasts a value over that axis, the backward pass
      will perform a ``psum`` of the corresponding gradient. Otherwise, the
      VJP will be per-example over named axes. For example, if ``'batch'``
      is a named batch axis, ``vjp(f, *args, reduce_axes=('batch',))`` will
      create a VJP function that sums over the batch while ``vjp(f, *args)``
      will create a per-example VJP.
    vjp_variables: The vjpfun will return a cotangent vector for all
      variable collections specified by this filter.
    variables: other variables collections that are available inside `fn` but
      do not receive a cotangent.
    rngs: the prngs that are available inside `fn`.

  Returns:
    If ``has_aux`` is ``False``, returns a ``(primals_out, vjpfun)`` pair, where
    ``primals_out`` is ``fn(*primals)``.
    ``vjpfun`` is a function from a cotangent vector with the same shape as
    ``primals_out`` to a tuple of cotangent vectors with the same shape as
    ``primals``, representing the vector-Jacobian product of ``fn`` evaluated at
    ``primals``. If ``has_aux`` is ``True``, returns a
    ``(primals_out, vjpfun, aux)`` tuple where ``aux`` is the auxiliary data
    returned by ``fn``.
  """
  def inner(scope_fn, repack_fn, variable_groups, rng_groups, *args):
    vjp_vars, other_vars = variable_groups
    @functools.wraps(fn)
    def wrapper(vjp_vars, *args):
      variable_groups = (vjp_vars, other_vars)
      scope = scope_fn(variable_groups, rng_groups)
      if has_aux:
        y, aux = fn(scope, *args)
      else:
        y = fn(scope, *args)
        aux = ()
      return y, (aux, repack_fn(scope))
    y, bwd, (aux, out_vars) = jax.vjp(
        wrapper, vjp_vars, *args,
        reduce_axes=reduce_axes, has_aux=True)
    treedef = jax.tree_util.tree_structure(scope)
    bwd = jax.tree_util.Partial(
        functools.partial(_bwd_wrapper, treedef), bwd)
    if has_aux:
      return (y, bwd, aux), out_vars
    else:
      return (y, bwd), out_vars
  return pack(
      inner, (vjp_variables, variables), (variables,), (rngs,),
      name='vjp',
      enable_kwargs=False)(scope, *primals)


def jvp(
    fn: Callable[..., Any],
    scope: Scope,
    primals,
    tangents,
    variable_tangents,
    variables: CollectionFilter = True,
    rngs: PRNGSequenceFilter = True,
    ) -> Tuple[Any, Any]:
  """A lifted version of ``jax.jvp``.

  See ``jax.jvp`` for the unlifted Jacobian-vector product (forward gradient).

  Note that no tangents are returned for variables. When variable tangents
  are required their value should be returned explicitly by `fn`
  using `scope.variables()`.

  Example::

    def learn_scale(scope, x):
      p = scope.param('scale', nn.initializers.zeros_init(), ())
      return p * x

    def f(scope, x):
      vars_t = jax.tree_util.tree_map(jnp.ones_like,
                                      scope.variables().get('params', {}))
      x, out_t = lift.jvp(
          learn_scale, scope, (x,), (jnp.zeros_like(x),),
          variable_tangents={'params': vars_t})
      return out_t

  Args:
    fn: The function to be transformed.
    scope: The scope(s) which should be lifted into the transform.
    primals: The primal values at which the Jacobian of ``fun`` should be
      evaluated. Should be either a tuple or a list of arguments,
      and its length should be equal to the number of positional parameters of
      ``fun``.
    tangents: The tangent vector for which the Jacobian-vector product should be
      evaluated. Should be either a tuple or a list of tangents, with the same
      tree structure and array shapes as ``primals``.
    variable_tangents: A dict or PyTree fo dicts with the same structure as
      scopes. Each entry in the dict specifies the tangents for a variable
      collection. Not specifying a collection in variable_tangents is
      equivalent to passing a zero vector as the tangent.
    variables: other variables collections that are available inside `fn` but
      do not receive a tangent.
    rngs: the prngs that are available inside `fn`.

  Returns:
    A ``(primals_out, tangents_out)`` pair, where ``primals_out`` is
    ``fun(*primals)``, and ``tangents_out`` is the Jacobian-vector product of
    ``function`` evaluated at ``primals`` with ``tangents``. The
    ``tangents_out`` value has the same Python tree structure and shapes as
    ``primals_out``.
  """
  def inner(scope_fn, repack_fn, variable_groups, rng_groups, *args):
    jvp_vars, other_vars = variable_groups
    @functools.wraps(fn)
    def wrapper(vars_primals, args):
      variable_groups = (vars_primals, other_vars)
      scope = scope_fn(variable_groups, rng_groups)
      y = fn(scope, *args)
      return y, repack_fn(scope)

    (y, out_vars), out_tangents = jax.jvp(wrapper, (jvp_vars, args),
                                          (variable_tangents, tangents))
    return (y, out_tangents[0]), out_vars
  # filter out empty tangent collections because JAX will error on non-equal
  # tree structure for example: {"params": {}} != {}.
  treedef = jax.tree_util.tree_structure(scope)

  variable_tangents = tuple({k: v  # pylint: disable=g-complex-comprehension
                             for k, v in vt.items()
                             if v}
                            for vt in treedef.flatten_up_to(variable_tangents))
  target = tuple(variable_tangents[0].keys())
  return pack(
      inner, (target, variables), (variables,), (rngs,),
      name='jvp', enable_kwargs=False)(scope, *primals)


def vmap(fn: Callable[..., Any],
         variable_axes: Mapping[CollectionFilter, InOutAxis],
         split_rngs: Mapping[PRNGSequenceFilter, bool],
         in_axes=0,
         out_axes=0,
         axis_size: Optional[int] = None,
         axis_name: Optional[str] = None,
         spmd_axis_name: Optional[str] = None,
         metadata_params: Dict[Any, Any] = {}) -> Callable[..., Any]:
  """A lifted version of ``jax.vmap``.

  See ``jax.vmap`` for the unlifted batch transform in Jax.

  ``vmap`` can be used to add a batch axis to a scope function.
  For example we could create a version of ``dense`` with
  a batch axis that does not share parameters::

    batch_dense = lift.vmap(
        nn.dense,
        in_axes=(0, None),
        variable_axes={'params': 0},
        split_rngs={'params': True})

  By using ``variable_axes={'params': 0}``, we indicate that the
  parameters themselves are mapped over and therefore not shared along
  the mapped axis. Consequently, we also split the 'params' RNG,
  otherwise the parameters would be initialized identically along
  the mapped axis.

  Similarly, ``vmap`` could be use to add a batch axis with parameter
  sharing::

    batch_foo = lift.vmap(
        foo,
        in_axes=0, out_axes=0,
        variable_axes={'params': None},
        split_rngs={'params': False})

  Here we use ``variable_axes={'params': None}`` to indicate the parameter
  variables are shared along the mapped axis. Consequently, the 'params'
  RNG must also be shared.

  Args:
    fn: the function to be transformed.
    variable_axes: the variable collections that are lifted into the
      batching transformation. Use `None` to indicate a broadcasted
      collection or an integer to map over an axis.
    split_rngs: Split PRNG sequences will be different for each index
      of the batch dimension. Unsplit PRNGs will be broadcasted.
    in_axes: Specifies the mapping of the input arguments (see `jax.vmap).
    out_axes: Specifies the mapping of the return value (see `jax.vmap).
    axis_size: Specifies the size of the batch axis. This only needs
      to be specified if it cannot be derived from the input arguments.
    axis_name: Specifies a name for the batch axis. Can be used together
      with parallel reduction primitives (e.g. `jax.lax.pmean`,
      `jax.lax.ppermute`, etc.)
    spmd_axis_name: Axis name added to any pjit sharding constraints appearing
      in `fn`. See also
      https://github.com/google/flax/blob/main/flax/linen/partitioning.py.
    metadata_params: arguments dict passed to AxisMetadata instances in the
      variable tree.

  Returns:
    A vectorized version of the input scope function.
  """
  variable_in_axes, variable_out_axes = _split_in_out_axes(variable_axes)
  variable_in_groups, variable_in_axes = _unzip2(variable_in_axes.items())
  variable_out_groups, variable_out_axes = _unzip2(variable_out_axes.items())
  rng_groups, rng_splits = _unzip2(split_rngs.items())
  rng_axes = tuple(0 if rng_split else None for rng_split in rng_splits)

  def inner(scope_fn, repack_fn, variable_groups, rng_groups, *args):
    def find_axis_size(axis, x):
      if axis is not None:
        leaves = jax.tree_util.tree_leaves(x)
        if leaves:
          return leaves[0].shape[axis]
      return ()

    # split rngs
    axis_sizes = jax.tree_util.tree_map(find_axis_size,
                                        (variable_in_axes, in_axes),
                                        (variable_groups, args))
    axis_sizes = set(jax.tree_util.tree_leaves(axis_sizes))
    if axis_size is None and len(axis_sizes) == 1:
      d_axis_size, = axis_sizes
    elif len(axis_sizes) > 1:
      raise ValueError(f'Inconsistent batch axis sizes: {axis_sizes}')
    elif axis_size is None:
      raise ValueError('axis_size should be specified manually.')
    else:
      d_axis_size = axis_size
    split_fn = lambda rng: random.split(rng, d_axis_size)

    rng_groups = tuple(
        tree_map_rngs(split_fn, rng_group) if split else rng_group
        for rng_group, split in zip(rng_groups, rng_splits))

    new_variable_groups = []
    for var_group, axis in zip(variable_groups, variable_in_axes):
      if axis is not None:
        new_variable_groups.append(meta.remove_axis(
            var_group, axis, metadata_params))
      else:
        new_variable_groups.append(var_group)
    variable_groups = tuple(new_variable_groups)

    @functools.partial(
        jax.vmap,
        in_axes=(variable_in_axes, rng_axes, in_axes),
        out_axes=(out_axes, variable_out_axes),
        axis_name=axis_name,
        axis_size=axis_size,
        spmd_axis_name=spmd_axis_name)
    @functools.wraps(fn)
    def mapped(variable_groups, rng_groups, args):
      scope = scope_fn(variable_groups, rng_groups)
      y = fn(scope, *args)
      return y, repack_fn(scope)

    y, vars_out = mapped(variable_groups, rng_groups, args)
    new_vars_out = []
    for var_group, axis in zip(vars_out, variable_out_axes):
      if axis is not None:
        new_vars_out.append(meta.add_axis(var_group, axis, metadata_params))
      else:
        new_vars_out.append(var_group)
    vars_out = tuple(new_vars_out)
    return y, vars_out

  return pack(
      inner, variable_in_groups, variable_out_groups, rng_groups,
      name='vmap')


ScanAxis = int
InOutScanAxis = Union[ScanAxis, In[ScanAxis], Out[ScanAxis]]

def scan(fn: Callable[..., Any],
         variable_axes: Mapping[CollectionFilter, InOutScanAxis] = {},
         variable_broadcast: CollectionFilter = False,
         variable_carry: CollectionFilter = False,
         split_rngs: Mapping[PRNGSequenceFilter, bool] = {},
         in_axes=0, out_axes=0,
         length: Optional[int] = None,
         reverse: bool = False,
         unroll: int = 1,
         data_transform: Optional[Callable[..., Any]] = None,
         metadata_params: Dict[Any, Any] = {},
         ) -> Callable[..., Any]:
  """A lifted version of ``jax.lax.scan``.

  See ``jax.lax.scan`` for the unlifted scan in Jax.

  To improve consistency with ``vmap``, this version of scan
  uses ``in_axes`` and ``out_axes`` to determine which arguments
  are scanned over and along which axis.

  ``scan`` distinguishes between 3 different types of values inside the loop:

  1. **scan**: a value that is iterated over in a loop. All scan values must
    have the same size in the axis they are scanned over. Scanned outputs
    will be stacked along the scan axis.
  2. **carry**: A carried value is updated at each loop iteration. It must
    have the same shape and dtype throughout the loop.
  3. **broadcast**: a value that is closed over by the loop. When a variable
    is broadcasted they are typically initialized inside the loop body but
    independent of the loop variables.

  The loop body should have the signature
  ``(scope, body, carry, *xs) -> (carry, ys)``, where ``xs`` and ``ys``
  are the scan values that go in and out of the loop.

  Example::

    scope.variable('counter', 'i', jnp.zeros, ())
    def body_fn(scope, c, x):
      counter = scope.variable('counter', 'i', jnp.zeros, ())
      counter.value += 1
      x = scope.child(nn.dense)(x, 1)
      return c, x

    _, ys = lift.scan(
        body_fn,
        variable_carry='counter',
        variable_broadcast='params',
        split_rngs={'params': False})(scope, (), xs)

  Args:
    fn: the function to be transformed.
    variable_axes: the variable collections that are scanned over.
    variable_broadcast: Specifies the broadcasted variable collections.
      A broadcasted variable should not depend on any computation that cannot b
      lifted out of the loop. This is typically used to define shared parameters
      inside the fn.
    variable_carry: Specifies the variable collections that are carried through
      the loop. Mutations to these variables are carried to the next iteration
      and will be preserved when the scan finishes.
    split_rngs: Split PRNG sequences will be different for each loop iterations.
      If split is False the PRNGs will be the same across iterations.
    in_axes: Specifies the axis to scan over for the arguments. Should be a
      prefix tree of the arguments. Use `flax.core.broadcast` to feed an entire
      input to each iteration of the scan body.
    out_axes: Specifies the axis to scan over for the return value. Should be a
      prefix tree of the return value.
    length: Specifies the number of loop iterations. This only needs
      to be specified if it cannot be derived from the scan arguments.
    reverse: If true, scan from end to start in reverse order.
    unroll: how many scan iterations to unroll within a single
      iteration of a loop (default: 1).
    data_transform: optional function to transform raw variable and rng groups,
      intended for inline SPMD annotations.
    metadata_params: arguments dict passed to AxisMetadata instances in the
      variable tree.

  Returns:
    The scan function with the signature
    ``(scope, carry, *xxs) -> (carry, yys)``, where ``xxs`` and ``yys`` are the
    scan values that go in and out of the loop.
  """
  from flax.linen.module import tabulate_context

  variable_in_axes, variable_out_axes = _split_in_out_axes(variable_axes)
  variable_in_groups, variable_in_axes = _unzip2(variable_in_axes.items())
  variable_out_groups, variable_out_axes = _unzip2(variable_out_axes.items())
  assert all(isinstance(ax, int) for ax in variable_in_axes)
  assert all(isinstance(ax, int) for ax in variable_out_axes)
  rng_groups, rng_splits = _unzip2(split_rngs.items())
  rng_axes = tuple(0 if rng_split else axes_scan.broadcast
                   for rng_split in rng_splits)

  def inner(scope_fn, repack_fn,
            variable_groups, rng_groups,
            init, *args):
    def find_length(axis, x):
      if axis is not axes_scan.broadcast:
        leaves = jax.tree_util.tree_leaves(x)
        if leaves:
          return leaves[0].shape[axis]
      return ()
    # split rngs
    lengths = jax.tree_util.tree_map(find_length, in_axes, args)
    lengths = set(jax.tree_util.tree_leaves(lengths))
    if length is None and len(lengths) == 1:
      d_length, = lengths
    elif len(lengths) > 1:
      raise ValueError(f'Inconsistent scan lengths: {lengths}')
    elif length is None:
      raise ValueError('length should be specified manually.')
    else:
      d_length = length
    split_fn = lambda rng: random.split(rng, d_length)

    rng_groups = tuple(
        tree_map_rngs(split_fn, rng_group) if split else rng_group
        for rng_group, split in zip(rng_groups, rng_splits))

    carry_vars_new_axes = 0
    scan_partial = lambda length, unroll: axes_scan.scan(
      scanned,
      in_axes=(variable_in_axes, rng_axes, in_axes),
      out_axes=(out_axes, variable_out_axes, carry_vars_new_axes),
      reverse=reverse, unroll=unroll, length=length)

    def scanned(broadcast_vars, carry, scan_variable_groups, rng_groups, args):

      carry_vars, c = carry

      variable_groups = (broadcast_vars, carry_vars) + scan_variable_groups
      if data_transform is not None:
        variable_groups, rng_groups = data_transform(variable_groups,
                                                     rng_groups)
      scope = scope_fn(variable_groups, rng_groups)
      c, y = fn(scope, c, *args)
      out_vars = repack_fn(scope)
      broadcast_vars_out = out_vars[0]
      carry_vars_out = out_vars[1]
      scan_vars = out_vars[2:]

      # compute new carry vars, these will be handled as outputs
      carry_vars_new = tuple(
        vars_diff(outputs, inputs) for outputs, inputs in zip(carry_vars_out, carry_vars))
      # remove new carry vars to maintain input shape
      carry_vars = tuple(
        vars_diff(outputs, new) for outputs, new in zip(carry_vars_out, carry_vars_new))

      # add immutable broadcast vars back to broadcast output
      # otherwise they won't be fed to the actual scan body
      for in_group, out_group in zip(broadcast_vars, broadcast_vars_out):
        for col in in_group:
          if col not in out_group:
            out_group[col] = in_group[col]
      return broadcast_vars_out, (carry_vars, c), (y, scan_vars, carry_vars_new)

    broadcast_vars = variable_groups[0]
    carry_vars = variable_groups[1]
    scan_vars = variable_groups[2:]
    new_scan_vars = []
    for scan_group, axis in zip(scan_vars, variable_in_axes):
      new_scan_vars.append(meta.remove_axis(scan_group, axis, metadata_params))

    # compute new carry vars
    with tabulate_context(add_call_info=False): # dont add call info while tracing
      carry_vars_new = jax.eval_shape(scan_partial(length, unroll),
        broadcast_vars, (carry_vars, init), tuple(new_scan_vars),
        rng_groups, args)[2][2]
    has_new_carry_vars = len(jax.tree_util.tree_leaves(carry_vars_new)) > 0

    if has_new_carry_vars:
      new_scan_vars0, rng_groups0, args0 = tree_map_upto_left(
        lambda axis, tree: jax.tree_map(
          lambda x: jax.lax.dynamic_slice_in_dim(x, 0, 1, axis),
          tree,
        ),
        left=(variable_in_axes, rng_axes, in_axes),
        right=(tuple(new_scan_vars), rng_groups, args)
      )
      # run scan for 1 step
      partial_length = 1 if length is not None else None
      with tabulate_context(add_call_info=False): # dont add call info on first step
        broadcast_vars, (carry_vars, init), (ys1, scan_vars1, carry_vars_new) = scan_partial(partial_length, 1)(
          broadcast_vars, (carry_vars, init), new_scan_vars0, rng_groups0, args0)
      # slice new carry vars and merge with existing
      carry_vars_new = jax.tree_map(lambda x: x[0], carry_vars_new)
      carry_vars = tuple(
        vars_merge(existing, new) for existing, new in zip(carry_vars, carry_vars_new))
      # slice rest of the inputs
      new_scan_vars_rest, rng_groups_rest, args_rest = tree_map_upto_left(
        lambda axis, tree: jax.tree_map(
          lambda x: jax.lax.dynamic_slice_in_dim(x, 1, x.shape[axis] - 1, axis),
          tree,
        ),
        left=(variable_in_axes, rng_axes, in_axes),
        right=(tuple(new_scan_vars), rng_groups, args)
      )
      # run scan on the rest of the inputs
      partial_length = length - 1 if length is not None else None
      broadcast_vars, (carry_vars, c), (ys_rest, scan_vars_rest, carry_vars_new) = scan_partial(partial_length, unroll)(
        broadcast_vars, (carry_vars, init), new_scan_vars_rest, rng_groups_rest, args_rest)
      # concat ys and scan_vars
      ys = tree_map_upto_left(
        lambda axis, tuple_tree: jax.tree_map(
          lambda a, b: jnp.concatenate((a, b), axis=axis),
          *tuple_tree,
        ),
        left=out_axes,
        right=(ys1, ys_rest),
      )
      scan_vars = tree_map_upto_left(
        lambda axis, tuple_tree: jax.tree_map(
          lambda a, b: jnp.concatenate((a, b), axis=axis),
          *tuple_tree,
        ),
        left=variable_out_axes,
        right=((scan_vars1, scan_vars_rest),),
      )[0]
    else:
      broadcast_vars, (carry_vars, c), (ys, scan_vars, carry_vars_new) = scan_partial(length, unroll)(
        broadcast_vars, (carry_vars, init), tuple(new_scan_vars),
        rng_groups, args)

    has_new_carry_vars = len(jax.tree_util.tree_leaves(carry_vars_new)) > 0
    assert not has_new_carry_vars

    new_scan_vars = []
    for scan_group, axis in zip(scan_vars, variable_out_axes):
      new_scan_vars.append(meta.add_axis(scan_group, axis, metadata_params))
    scan_vars = tuple(new_scan_vars)
    out_vars = (broadcast_vars, carry_vars) + scan_vars
    return (c, ys), out_vars

  return pack(
      inner,
      (variable_broadcast, variable_carry) + variable_in_groups,
      (variable_broadcast, variable_carry) + variable_out_groups,
      rng_groups,
      name='scan')


C = TypeVar('C')


def while_loop(cond_fn: Callable[[Scope, C], bool],
               body_fn: Callable[[Scope, C], C],
               scope: Scope, init: C,
               carry_variables: CollectionFilter = False,
               broadcast_variables: CollectionFilter = True,
               split_rngs: Mapping[PRNGSequenceFilter, bool] = {}) -> C:
  """Lifted version of jax.lax.while_loop.

  The lifted scope is passed to `cond_fn` and `body_fn`.
  Broadcasted variables are immutable. The carry variable are
  mutable but cannot change shape and dtype.
  This also means you cannot initialize variables inside
  the body. Consider calling `body_fn` once manually before
  calling `while_loop` if variable initialization is required.

  Example::

    def f(scope, x):
      def cond_fn(scope, c):
        return scope.get_variable('state', 'acc') < 10
      def body_fn(scope, c):
        acc = scope.variable('state', 'acc')
        acc += 1
        y = scope.child(nn.dense)(c, c.shape[-1])
        return y

      c = x
      c = body_fn(scope, c)
      return lift.while_loop(cond_fn, body_fn, scope, (),
                             carry_variables='state')

  Args:
    cond_fn: Should return True as long as the loop should continue.
    body_fn: The body of the while loop.
    scope: The scope(s) which should be lifted into the loop.
    init: The initial state passed to the loop
    carry_variables: collections that are carried through the loop
      and are therefore mutable (default: none).
    broadcast_variables: collections that are closed over and are
      therefore read-only (default: all collections)
    split_rngs: Split PRNG sequences will be different for each loop iterations.
      If split is False the PRNGs will be the same across iterations.
  Returns:
    The final state after executing the while loop.
  """
  rng_groups, rng_splits = _unzip2(split_rngs.items())

  def inner(scope_fn, repack_fn,
            variable_groups, rng_groups):
    carry_variables, broadcast_variables = variable_groups

    def make_loop_rngs(i):
      local_rng_groups = []
      for rng_group, rng_split in zip(rng_groups, rng_splits):
        if rng_split:
          rng_group = tree_map_rngs(lambda rng: random.fold_in(rng, i),
                                    rng_group)
        local_rng_groups.append(rng_group)
      return local_rng_groups

    def cond_wrapper(c):
      i, carry_variables, carry = c
      scope = scope_fn((carry_variables, broadcast_variables),
                       make_loop_rngs(-i),
                       mutable_filter=False)
      return cond_fn(scope, carry)

    def body_wrapper(c):
      i, carry_variables, carry = c
      scope = scope_fn((carry_variables, broadcast_variables),
                       make_loop_rngs(i))
      carry = body_fn(scope, carry)
      carry_variables, = repack_fn(scope)
      return (i + 1, carry_variables, carry)

    c = (0, carry_variables, init)
    _, carry_variables, carry = jax.lax.while_loop(cond_wrapper, body_wrapper,
                                                   c)
    return carry, (carry_variables,)

  return pack(
      inner,
      (carry_variables, broadcast_variables),
      (carry_variables,),
      rng_groups,
      name='while_loop')(scope)


def cond(pred: Any,
         true_fun: Callable[..., C], false_fun: Callable[..., C],
         scope: Scope, *operands,
         variables: CollectionFilter = True,
         rngs: PRNGSequenceFilter = True) -> C:
  """Lifted version of ``jax.lax.cond``.

  The returned values from ``true_fun`` and ``false_fun``
  must have the same Pytree structure, shapes, and dtypes.
  The variables created or updated inside the
  branches must also have the same structure.
  Note that this constraint is violated when
  creating variables or submodules in only one branch.
  Because initializing variables in just one branch
  causes the paramater structure to be different.

  Example::

    def cond_example(scope, x, pred):
      scope.variable('state', 'true_count', lambda: 0)
      scope.variable('state', 'false_count', lambda: 0)
      def true_fn(scope, x):
        scope.variable('state', 'true_count').value += 1
        return scope.child(nn.dense)(x, 2)
      def false_fn(scope, x):
        scope.variable('state', 'false_count').value += 1
        return -scope.child(nn.dense)(x, 2)
      return lift.cond(pred, true_fn, false_fn, scope, x)


  Args:
    pred: determines if true_fun or false_fun is evaluated.
    true_fun: The function evalauted when ``pred`` is `True`.
      The signature is (Scope, *operands) -> T.
    false_fun: The function evalauted when ``pred`` is `False`.
      The signature is (Scope, *operands) -> T.
    scope: A Scope or Pytree of scopes to pass
    *operands: The arguments passed to ``true_fun`` and ``false_fun``
    variables: The variable collections passed to the conditional
      branches (default: all)
    rngs: The PRNG sequences passed to the conditionals (default: all)
  Returns:
    The result of the evaluated branch (``true_fun`` or ``false_fun``).
  """
  branches = [true_fun, false_fun]
  def inner(scope_fn, repack_fn,
            variable_groups, rng_groups):
    def branch_wrapper(branch_fn, *operands):
      scope = scope_fn(variable_groups, rng_groups)
      y = branch_fn(scope, *operands)
      return y, repack_fn(scope)
    pure_branches = [
        functools.partial(branch_wrapper, branch_fn)
        for branch_fn in branches]
    return jax.lax.cond(
        pred, pure_branches[0], pure_branches[1], *operands)

  return pack(
      inner,
      (variables,),
      (variables,),
      (rngs,),
      name='cond')(scope)


def switch(index: Any,
           branches: Sequence[Callable[..., C]],
           scope: Scope, *operands,
           variables: CollectionFilter = True,
           rngs: PRNGSequenceFilter = True) -> C:
  """Lifted version of ``jax.lax.switch``.

  The returned values from ``branches``
  must have the same Pytree structure, shapes, and dtypes.
  The variables created or updated inside the
  branches must also have the same structure.
  Note that this constraint is violated when
  creating variables or submodules in only one branch.
  Because initializing variables in just one branch
  causes the parameter structure to be different.

  Example::

    def switch_example(scope, x, index):
      scope.variable('state', 'a_count', lambda: 0)
      scope.variable('state', 'b_count', lambda: 0)
      scope.variable('state', 'c_count', lambda: 0)
      def a_fn(scope, x):
        scope.variable('state', 'a_count').value += 1
        return scope.child(nn.dense)(x, 2)
      def b_fn(scope, x):
        scope.variable('state', 'b_count').value += 1
        return -scope.child(nn.dense)(x, 2)
      def c_fn(scope, x):
        scope.variable('state', 'c_count').value += 1
        return scope.child(nn.dense)(x, 2)
      return lift.switch(index, [a_fn, b_fn, c_fn], scope, x)

  If you want to have a different parameter structure for each branch
  you should run all branch on initialization before calling switch::

    def multihead_switch_example(scope, x, index):
      def a_fn(scope, x):
        x = scope.child(nn.dense)(x, 10)
        x = scope.child(nn.dense)(x, 7)
        return scope.child(nn.dense)(x, 5)
      def b_fn(scope, x):
        x = scope.child(nn.dense)(x, 11)
        return scope.child(nn.dense)(x, 5)
      def c_fn(scope, x):
        return scope.child(nn.dense)(x, 5)

      branches = [a_fn, b_fn, c_fn]

      # run all branches on init
      if scope.is_mutable_collection('params'):
        for branch in branches:
          _ = branch(scope, x)

      return lift.switch(index, branches, scope, x)

  Args:
    index: Integer scalar type, indicating which branch function to apply.
    branches: Sequence of functions to be applied based on index.
      The signature of each function is (Scope, *operands) -> T.
    scope: A Scope or Pytree of scopes to pass
    *operands: The arguments passed to ``true_fun`` and ``false_fun``
    variables: The variable collections passed to the conditional
      branches (default: all)
    rngs: The PRNG sequences passed to the conditionals (default: all)
  Returns:
    The result of the evaluated branch.
  """

  def inner(scope_fn, repack_fn,
            variable_groups, rng_groups):
    def branch_wrapper(branch_fn, *operands):
      scope = scope_fn(variable_groups, rng_groups)
      y = branch_fn(scope, *operands)
      return y, repack_fn(scope)
    pure_branches = [
        functools.partial(branch_wrapper, branch_fn)
        for branch_fn in branches]
    return jax.lax.switch(index, pure_branches, *operands)

  return pack(
      inner,
      (variables,),
      (variables,),
      (rngs,),
      name='switch')(scope)


def custom_vjp(fn: Callable[..., Any],
               forward_fn: Callable[..., Any],
               backward_fn: Callable[..., Any],
               grad_vars: CollectionFilter = 'params',
               nondiff_argnums=()):
  """Lifted version of `jax.custom_vjp`.

  `forward_fn` and `backward_fn` together define a custom vjp for `fn`.
  The original `fn` will run in case a vjp (backward gradient) is not computed.

  The `forward_fn` receives the same arguments as `fn` but is expected to return
  a tuple containing the output of `fn(scope, *args)` and the residuals that are
  passed to `backward_fn`.

  The `backward_fn` receives the nondiff arguments, residuals, and the output
  tangents. It should return a tuple containing the variable and input tangents.

  Note that the vjp function returned by `lift.vjp` can be passed as residual
  and used in the `backward_fn`. The scope is unavailable during the backward
  pass. If the scope is required in `backward_fn`, a snapshot of the variables
  can be taken and returned as a residual in the `forward_fn`.

  Example::

    f = nn.dense

    def fwd(scope, x, features):
      y, vjp_fn = lift.vjp(partial(f, features=features), scope, x)
      return y, vjp_fn

    def bwd(features, vjp_fn, y_t):
      params_t, *inputs_t = vjp_fn(y_t)
      params_t = jax.tree_util.tree_map(jnp.sign, params_t)
      return (params_t, *inputs_t)

    dense_sign_grad = lift.custom_vjp(
        f, forward_fn=fwd, backward_fn=bwd, nondiff_argnums=(2,))

  Args:
    fn: The function to define a custom_vjp for. The first argument
      should be a ``Module`` instance.
    forward_fn: A function with the same arguments as `fn` returning an tuple
      with the original output and the residuals that will be passed to
      `backward_fn`.
    backward_fn: arguments are passed as (*nondiff_args, residuals, tangents)
      The function should return a tuple containing the tangents for the
      variable in the collections specified by `grad_vars` and the input
      arguments (except the scope and nondiff args).
    grad_vars: The collections for which a vjp will be computed
      (default: "params").
    nondiff_argnums: arguments for which no vjp is computed.
  Returns:
    A function with the same signature as `fn` with the custom vjp.
  """
  def inner(scope_fn, repack_fn, variable_groups, rng_groups, *args):
    grad_variables, other_variables = variable_groups
    scopes_treedef = None

    def f(grad_variables, *args):
      scope = scope_fn((grad_variables, other_variables), rng_groups)
      y = fn(scope, *args)
      vars_out = repack_fn(scope)
      return y, vars_out
    f = jax.custom_vjp(f, nondiff_argnums=nondiff_argnums)

    def f_fwd(grad_variables, *args):
      nonlocal scopes_treedef
      scopes = scope_fn((grad_variables, other_variables), rng_groups)
      scopes_treedef = jax.tree_util.tree_structure(scopes)
      y, res = forward_fn(scopes, *args)
      vars_out = repack_fn(scopes)
      return (y, vars_out), res

    def f_bwd(*args):
      # the backward function does not pass a lifted scope to the user.
      # Currently, there is no way to have side effects flow out of backward
      # pass. Even without mutation variables would be ill-defined. For example,
      # would we take a snapshot of the variables before or after calling
      # `forward_fn`?
      nondiff_args = args[:-2]
      res, g = args[-2:]  # pylint: disable=unbalanced-tuple-unpacking
      g_y, _ = g
      var_t, *inputs_t = backward_fn(*nondiff_args, res, g_y)
      assert scopes_treedef is not None, 'backward called before forward?!'
      var_t = tuple(scopes_treedef.flatten_up_to(var_t))
      return (var_t, *inputs_t)

    f.defvjp(f_fwd, f_bwd)

    return f(grad_variables, *args)

  variable_in_groups = (grad_vars, True)
  variable_out_groups = (grad_vars, True)
  rng_groups = (True,)
  return pack(
      inner, variable_in_groups, variable_out_groups, rng_groups,
      name='custom_vjp')


def checkpoint(fn: Callable[..., Any],
               variables: CollectionFilter = True,
               rngs: PRNGSequenceFilter = True,
               concrete: bool = False,
               prevent_cse: bool = True,
               static_argnums: Union[int, Tuple[int, ...]] = (),
               policy: Optional[Callable[..., bool]] = None,
               ) -> Callable[..., Any]:
  """Lifted version of ``jax.checkpoint``.

  This function is aliased to ``lift.remat`` just like ``jax.remat``.

  Args:
    fn: scope function for which intermediate computations should be
    re-computed when computing gradients.
    variables: The variable collections that are lifted. By default all
      collections are lifted.
    rngs: The PRNG sequences that are lifted. By default all PRNG sequences
      are lifted.
    concrete: Optional, boolean indicating whether ``fun`` may involve
      value-dependent Python control flow (default False). Support for such
      control flow is optional, and disabled by default, because in some
      edge-case compositions with :func:`jax.jit` it can lead to some extra
      computation.
    prevent_cse: Optional, boolean indicating whether to prevent common
      subexpression elimination (CSE) optimizations in the HLO generated from
      differentiation. This CSE prevention has costs because it can foil other
      optimizations, and because it can incur high overheads on some backends,
      especially GPU. The default is True because otherwise, under a ``jit`` or
      ``pmap``, CSE can defeat the purpose of this decorator. But in some
      settings, like when used inside a ``scan``, this CSE prevention mechanism
      is unnecessary, in which case ``prevent_cse`` can be set to False.
    static_argnums: Optional, int or sequence of ints, indicates which argument
      values on which to specialize for tracing and caching purposes. Specifying
      arguments as static can avoid ConcretizationTypeErrors when tracing, but
      at the cost of more retracing overheads.
    policy: Experimental checkpoint policy, see ``jax.checkpoint``.
  Returns:
    A wrapped version of ``fn``. When computing gradients intermediate
    computations will be re-computed when computing gradients.
  """
  def inner(scope_fn, repack_fn, variable_groups, rng_groups, *args, **kwargs):
    # add 2 to each static_argnums because we add two initial arguments to rematted
    static_argnums_ = jax.tree_util.tree_map(lambda x: x + 2, static_argnums)
    @functools.partial(jax.remat,
                       concrete=concrete, static_argnums=static_argnums_,
                       prevent_cse=prevent_cse, policy=policy)
    @functools.wraps(fn)
    def rematted(variable_groups, rng_groups, *args, **kwargs):
      scope = scope_fn(variable_groups, rng_groups)
      y = fn(scope, *args, **kwargs)
      return y, repack_fn(scope)

    return rematted(variable_groups, rng_groups, *args, **kwargs)

  return pack(
      inner, (variables,), (variables,), (rngs,),
      name='remat',
      enable_kwargs=True)


remat = checkpoint


def _hashable_filter(x):
  """Hashable version of CollectionFilter."""
  if isinstance(x, Iterable):
    return tuple(x)  # convert un-hashable list & sets to tuple
  if isinstance(x, DenyList):
    return DenyList(_hashable_filter(
        x.deny))  # convert inner filter recursively
  return x


def jit(fn: Callable[..., Any],
        variables: CollectionFilter = True,
        rngs: PRNGSequenceFilter = True,
        static_argnums: Union[int, Iterable[int]] = (),
        donate_argnums: Union[int, Iterable[int]] = (),
        device=None,
        backend: Union[str, None] = None,
        ) -> Callable[..., Any]:
  """Lifted version of ``jax.jit``.

  Args:
    fn: Scope function to be jitted.
    variables: The variable collections that are lifted. By default all
      collections are lifted.
    rngs: The PRNG sequences that are lifted. By default all PRNG sequences
      are lifted.
    static_argnums: An int or collection of ints specifying which positional
      arguments to treat as static (compile-time constant). Operations that only
      depend on static arguments will be constant-folded in Python (during
      tracing), and so the corresponding argument values can be any Python
      object. Static arguments should be hashable, meaning both ``__hash__`` and
      ``__eq__`` are implemented, and immutable. Calling the jitted function
      with different values for these constants will trigger recompilation. If
      the jitted function is called with fewer positional arguments than
      indicated by ``static_argnums`` then an error is raised. Arguments that
      are not arrays or containers thereof must be marked as static.
      Defaults to ().
    donate_argnums: Specify which arguments are "donated" to the computation.
      It is safe to donate arguments if you no longer need them once the
      computation has finished. In some cases XLA can make use of donated
      buffers to reduce the amount of memory needed to perform a computation,
      for example recycling one of your input buffers to store a result. You
      should not reuse buffers that you donate to a computation, JAX will raise
      an error if you try to.
    device: This is an experimental feature and the API is likely to change.
      Optional, the Device the jitted function will run on. (Available devices
      can be retrieved via :py:func:`jax.devices`.) The default is inherited
      from XLA's DeviceAssignment logic and is usually to use
      ``jax.devices()[0]``.
    backend: a string representing the XLA backend: ``'cpu'``, ``'gpu'``, or
      ``'tpu'``.

  Returns:
    A wrapped version of ``fn``, set up for just-in-time compilation.
  """
  if not isinstance(static_argnums, Iterable):
    static_argnums = (static_argnums,)
  if not isinstance(donate_argnums, Iterable):
    donate_argnums = (donate_argnums,)
  # offset argnums by two because first argument in the original function is the
  # scope while jitted has 3 functions before the user arguments.
  static_argnums = (0,) + tuple(i + 2 for i in static_argnums if i > 0)
  donate_argnums = tuple(i + 2 for i in donate_argnums if i > 0)

  # Close over scope_fn & repack_fn to avoid recompilation
  # this is impure but we use the fingerprint arg to differentiate between cases
  # where scope_fn or repack_fn actually produce non-identical results.
  scope_fn = None  # type: Optional[Callable]
  repack_fn = None  # type: Optional[Callable]
  @functools.partial(jax.jit,
                     static_argnums=static_argnums,
                     donate_argnums=donate_argnums,
                     device=device, backend=backend)
  @functools.wraps(fn)
  def jitted(fingerprint, variable_groups, rng_groups, *args):
    nonlocal scope_fn, repack_fn
    # fingerprint is only used to differentiate the cache signature for cases
    # where different collections are mutable.
    del fingerprint
    scope = scope_fn(variable_groups, rng_groups)  # pylint: disable=not-callable
    y = fn(scope, *args)
    return y, repack_fn(scope)  # pylint: disable=not-callable

  def inner(scope_fun, repack_fun, variable_groups, rng_groups, *args):
    nonlocal scope_fn, repack_fn
    try:
      scope_fn = scope_fun
      repack_fn = repack_fun
      scopes = jax.tree_util.tree_leaves(scope_fn(variable_groups, rng_groups))
      mutable = tuple(_hashable_filter(scope.mutable) for scope in scopes)
      return jitted(mutable, variable_groups, rng_groups, *args)
    finally:
      scope_fn, repack_fn = None, None

  return pack(inner, (variables,), (variables,), (rngs,), name='jit')


def remat_scan(
    body_fn: Callable[..., Any],
    lengths: Sequence[int],
    policy: Optional[Callable[..., bool]] = None,
    variable_broadcast: CollectionFilter = False,
    variable_carry: CollectionFilter = False,
    variable_axes: Mapping[CollectionFilter, InOutScanAxis] = {True: 0},
    split_rngs: Mapping[PRNGSequenceFilter, bool] = {True: True}
) -> Callable[..., Any]:
  """Combines `lift.remat` and `lift.scan` for memory efficiency and constant time compilation.

  ``remat_scan`` allows for constant compile times and sublinear
  memory usage with respect to model depth. At a small constant
  penalty. This is typically beneficial for very deep models.

  Example::

    def body_fn(scope, x):
      return nn.dense(scope, x, features=x.shape[-1])
    # 100x dense with O(sqrt(N)) memory for gradient computation
    y = lift.remat_scan(body_fn, lengths=(10, 10))(scope, x)

  Args:
    body_fn: Scope function to be repeated using a (nested scan)
    lengths: number of loop iterations at the given level. The total number of
      iterations `n = prod(lengths)`. each loop is rematerialized. This way the
      memory consumption is proportional to `n^(1 / d)` where `d =
      len(lengths)`. Minimal memory consumptions requires tuning the lengths
      such that the same amount of memory is consumed at each level of the
      nested loop.
    policy: Experimental checkpoint policy, see ``jax.checkpoint``.
    variable_broadcast: Specifies the broadcasted variable collections. A
      broadcasted variable should not depend on any computation that cannot be
      lifted out of the loop. This is typically used to define shared parameters
      inside the fn.
    variable_carry: Specifies the variable collections that are carried through
      the loop. Mutations to these variables are carried to the next iteration
      and will be preserved when the scan finishes.
    variable_axes: the variable collections that are scanned over.
    split_rngs: Split PRNG sequences will be different for each loop iterations.
      If split is False the PRNGs will be the same across iterations.
  Returns:
    A wrapped version of ``body_fn`` that repeats itself prod(lengths) times.
  """
  # TODO(jheek) should remat scan have scan inputs/outputs?
  scan_fn = functools.partial(
      scan,
      variable_broadcast=variable_broadcast,
      variable_carry=variable_carry,
      variable_axes=variable_axes,
      split_rngs=split_rngs)
  if len(lengths) == 1:
    def wrapper(scope, carry):
      return body_fn(scope, carry), ()
    fn = lambda scope, c: scan_fn(wrapper, length=lengths[0])(scope, c)[0]
  else:
    @functools.partial(remat, policy=policy, prevent_cse=False)
    def inner_loop(scope, carry):
      carry = remat_scan(body_fn, lengths[1:], policy,
                         variable_broadcast, variable_carry,
                         variable_axes, split_rngs)(scope, carry)
      return carry, ()
    fn = lambda scope, c: scan_fn(inner_loop, length=lengths[0])(scope, c)[0]
  return fn


def _unzip2(xs):
  ys = tuple(zip(*xs))
  return ys if ys else ((), ())

def vars_diff(a, b):
  a = traverse_util.flatten_dict(a, sep='/')
  b = traverse_util.flatten_dict(b, sep='/')

  c = {
    path: value
    for path, value in a.items()
    if path not in b
  }
  c = traverse_util.unflatten_dict(c, sep='/')
  return c

def vars_merge(a, b):
  a = traverse_util.flatten_dict(a, sep='/')
  b = traverse_util.flatten_dict(b, sep='/')
  a.update(b)
  c = traverse_util.unflatten_dict(a, sep='/')
  return c

def tree_map_upto_left(
    f: Callable[[Any, Any], Any], left: Any, right: Any
) -> Any:
    leaves_left, treedef = jax.tree_util.tree_flatten(left)
    leaves_right = treedef.flatten_up_to(right)

    return treedef.unflatten(
        f(left_leaf, right_leaf)
        for left_leaf, right_leaf in zip(leaves_left, leaves_right)
    )