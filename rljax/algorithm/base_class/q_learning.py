import os
from abc import abstractmethod

import numpy as np

from rljax.algorithm.base_class.algoirithm import OffPolicyAlgorithm
from rljax.util import fake_state, load_params, save_params


class QLearning(OffPolicyAlgorithm):
    """
    Base class for discrete Q-learning algorithms.
    """

    def __init__(
        self,
        num_agent_steps,
        state_space,
        action_space,
        seed,
        max_grad_norm,
        gamma,
        nstep,
        buffer_size,
        use_per,
        batch_size,
        start_steps,
        update_interval,
        update_interval_target,
        eps,
        eps_eval,
        eps_decay_steps,
    ):
        super(QLearning, self).__init__(
            num_agent_steps=num_agent_steps,
            state_space=state_space,
            action_space=action_space,
            seed=seed,
            max_grad_norm=max_grad_norm,
            gamma=gamma,
            nstep=nstep,
            buffer_size=buffer_size,
            use_per=use_per,
            batch_size=batch_size,
            start_steps=start_steps,
            update_interval=update_interval,
            update_interval_target=update_interval_target,
        )
        self.eps = eps
        self.eps_eval = eps_eval
        self.eps_decay_steps = eps_decay_steps
        # Define fake input.
        if not hasattr(self, "fake_args"):
            self.fake_args = (fake_state(state_space),)
        # If _forward() method uses random key or not.
        if not hasattr(self, "use_key_forward"):
            self.use_key_forward = False
        # Number of random keys for _loss() method.
        if not hasattr(self, "num_keys_loss"):
            self.num_keys_loss = 0

    def select_action(self, state):
        if np.random.rand() < self.eps_eval:
            action = self.action_space.sample()
        else:
            action = self.forward(state[None, ...])
            action = np.array(action[0])
        return action

    def explore(self, state):
        if np.random.rand() < self.eps_train:
            action = self.action_space.sample()
        else:
            action = self.forward(state[None, ...])
            action = np.array(action[0])
        return action

    def forward(self, state):
        return self._forward(params=self.params, state=state, **self.kwargs_forward)

    @abstractmethod
    def _forward(self, params, state, **kwargs):
        pass

    @property
    def eps_train(self):
        if self.agent_step > self.eps_decay_steps:
            return self.eps
        return 1.0 + (self.eps - 1.0) / self.eps_decay_steps * self.agent_step

    @property
    def kwargs_forward(self):
        return {"key": next(self.rng)} if self.use_key_forward else {}

    @property
    def kwargs_update(self):
        return {"key_list": self.get_key_list(self.num_keys_loss)} if self.num_keys_loss else {}

    def save_params(self, save_dir):
        save_params(self.params, os.path.join(save_dir, "params.npz"))

    def load_params(self, save_dir):
        self.params = self.params_target = load_params(os.path.join(save_dir, "params.npz"))