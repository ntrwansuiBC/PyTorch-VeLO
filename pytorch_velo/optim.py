import inspect
from typing import Callable, Optional, Union

from learned_optimization.optimizers.base import (
    Optimizer as LearnedOptimizerBase,
)
from learned_optimization.research.general_lopt.prefab import (
    LearnedOptimizer,
)
from learned_optimization.research.general_lopt import pretrained_optimizers
import jax
import jax.dlpack
import jax.numpy as jnp
from jaxlib.xla_extension import Device
try:
    from jaxlib.xla_extension import GpuDevice
except ImportError:
    GpuDevice = None
import torch as th
import torch.utils.dlpack

if GpuDevice is not None:
    JAXDevice = Union[Device, GpuDevice]
else:
    JAXDevice = Device
    jax.default_backend = 'gpu'
    jax.default_device(jax.devices('gpu')[0])
    jax.config.update("jax_default_device", jax.devices('gpu')[0])
LossClosure = Union[
    Callable[[], th.Tensor],
    Callable[[], float],
]

_DEFAULT_LOPT_FN = (
    inspect.signature(LearnedOptimizer).parameters['base_lopt_fn'].default
)

import os
os.environ['CUDA_VISIBLE_DEVICES']='0'

class DeviceMappingError(ValueError):
    def __init__(self, msg: Optional[str] = None) -> None:
        if msg is None:
            msg = 'could not map pytorch device to jax device'
        super().__init__(msg)


def get_lopt_fn(opt_name: str, force=False) -> Callable:
    assert force or opt_name in pretrained_optimizers.opt_names, (
        'can only safely get pre-named optimizer functions. '
        'Supply `force=True` to ignore this error.'
    )
    lopt_name = opt_name.replace('.', '_').replace('-', '_')
    fn = getattr(pretrained_optimizers, lopt_name)
    assert callable(fn), f'{opt_name} does not resolve in a callable'
    return fn


def _th_device_to_jax(device: th.device) -> JAXDevice:
    device_index = device.index if device.index is not None else 0
    jax_devices = jax.local_devices(backend=device.type)
    jax_device = jax_devices[device_index]
    # We iterate explicitly in case the index does not match.
    if jax_device.id != device_index:
        jax_device = next(
            (
                jax_device
                for jax_device in jax_devices
                if jax_device.id == device_index
            ),
            None,
        )
        if jax_device is None:
            raise DeviceMappingError()
    return jax_device


def _th_to_jax(tensor: th.Tensor) -> jnp.ndarray:
    return jax.dlpack.from_dlpack(
        th.utils.dlpack.to_dlpack(tensor.detach()),
    )


def _jax_to_th(array: jnp.ndarray) -> th.Tensor:
    return th.utils.dlpack.from_dlpack(
        jax.dlpack.to_dlpack(array),
    )


class VeLO(th.optim.Optimizer):
    def __init__(
            self,
            params,
            num_training_steps: int,
            weight_decay: float = 0.0,
            max_training_steps: int = 150_000,
            base_lopt_fn: Callable[[], LearnedOptimizerBase] = (
                _DEFAULT_LOPT_FN
            ),
            model_state: Optional[th.Tensor] = None,
            seed: int = 0,
            device: Union[th.device, str, None] = None,
    ) -> None:
        defaults = {}
        super().__init__(params, defaults)

        if device is None:
            device = self.param_groups[0]['params'][0].device
        elif isinstance(device, str):
            device = th.device(device)
        jax_device = _th_device_to_jax(device)

        with jax.default_device(jax_device):
            self.opt = LearnedOptimizer(
                num_training_steps=num_training_steps,
                weight_decay=weight_decay,
                max_training_steps=max_training_steps,
                base_lopt_fn=base_lopt_fn,
            )

        jax_params = {
            str(i): [
                _th_to_jax(p.ravel())
                for p in group['params']
            ]
            for (i, group) in enumerate(self.param_groups)
        }
        jax_model_state = (
            _th_to_jax(model_state.ravel())
            if model_state is not None
            else model_state
        )

        rng_key = jax.random.PRNGKey(seed)
        self.state['rng_key'], init_key = jax.random.split(rng_key)
        self.state['opt_state'] = self.opt.init(
            jax_params,
            model_state=jax_model_state,
            num_steps=num_training_steps,
            key=init_key,
        )

    @th.no_grad()
    def step(
            self,
            closure: LossClosure,
    ) -> Union[th.Tensor, float, None]:
        loss = None
        model_state = None
        
        if closure is not None:
            with th.enable_grad():
                closure_result = closure()
                if isinstance(closure_result, tuple):
                    assert len(closure_result) == 2, (
                        'closure must return a 2-tuple if not returning a scalar'
                    )
                    loss, model_state = closure_result
                elif isinstance(closure_result, th.Tensor):
                    loss = closure_result
                    assert loss.numel() == 1, 'loss must be a scalar'
                    model_state = None
                else:
                    raise TypeError(
                        'closure returned type that is not handled: '
                        + str(type(closure_result))
                    )

        # jax_grad = {
        #     str(i): [_th_to_jax(p.grad.ravel()) for p in group['params'] if p.gard is None] 
        #     for (i, group) in enumerate(self.param_groups)
        # }

        jax_grad = {}
        for (i, group) in enumerate(self.param_groups):
            jax_grad[str(i)] = []
            for p in group['params']:
                if p.grad is None:
                    jax_grad[str(i)].append(None)
                else:
                    jax_grad[str(i)].append(_th_to_jax(p.grad.ravel()))
                    
        jax_model_state = (
            _th_to_jax(model_state.ravel())
            if model_state is not None
            else model_state
        )

        if loss is not None:
            loss = _th_to_jax(loss)
        else:
            loss = jnp.array(0.0)

        jax.device_put(jax_grad)
        jax.device_put(jax_model_state)
        jax.device_put(loss)
        
        self.state['rng_key'], opt_key = jax.random.split(
            self.state['rng_key'])
        
        jax.device_put(opt_key)
        # jax.device_put(self.state['rng_key'])

        self.state['opt_state'] = self.opt.update(
            self.state['opt_state'],
            jax_grad,
            model_state=jax_model_state,
            loss=loss,
            key=opt_key,
        )

        for (i, group) in enumerate(self.param_groups):
            for (param, jax_param) in zip(
                    group['params'],
                    self.opt.get_params(self.state['opt_state'])[str(i)],
            ):
                param.data[:] = _jax_to_th(jax_param).reshape(param.shape)
        return loss
