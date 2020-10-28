from functools import partial
from typing import Any, Tuple

import haiku as hk
import jax
import jax.numpy as jnp
import numpy as np
from jax.experimental import optix

from rljax.algorithm.base import OffPolicyActorCritic
from rljax.network import ContinuousQuantileFunction, StateDependentGaussianPolicy
from rljax.util import clip_gradient_norm, quantile_loss, reparameterize_gaussian_and_tanh


class TQC(OffPolicyActorCritic):
    name = "TQC"

    def __init__(
        self,
        num_agent_steps,
        state_space,
        action_space,
        seed,
        max_grad_norm=None,
        gamma=0.99,
        nstep=1,
        buffer_size=10 ** 6,
        batch_size=256,
        start_steps=10000,
        update_interval=1,
        tau=5e-3,
        lr_actor=3e-4,
        lr_critic=3e-4,
        lr_alpha=3e-4,
        units_actor=(256, 256),
        units_critic=(256, 256),
        d2rl=False,
        num_critics=5,
        num_quantiles=25,
        num_quantiles_to_drop=0,
    ):
        super(TQC, self).__init__(
            num_agent_steps=num_agent_steps,
            state_space=state_space,
            action_space=action_space,
            seed=seed,
            max_grad_norm=max_grad_norm,
            gamma=gamma,
            nstep=nstep,
            buffer_size=buffer_size,
            use_per=False,
            batch_size=batch_size,
            start_steps=start_steps,
            update_interval=update_interval,
            tau=tau,
        )
        if d2rl:
            self.name += "-D2RL"

        def critic_fn(s, a):
            return ContinuousQuantileFunction(
                num_critics=num_critics,
                hidden_units=units_critic,
                num_quantiles=num_quantiles,
                d2rl=d2rl,
            )(s, a)

        def actor_fn(s):
            return StateDependentGaussianPolicy(
                action_space=action_space,
                hidden_units=units_actor,
                d2rl=d2rl,
            )(s)

        # Critic.
        self.critic = hk.without_apply_rng(hk.transform(critic_fn))
        self.params_critic = self.params_critic_target = self.critic.init(next(self.rng), self.fake_state, self.fake_action)
        opt_init, self.opt_critic = optix.adam(lr_critic)
        self.opt_state_critic = opt_init(self.params_critic)

        # Actor.
        self.actor = hk.without_apply_rng(hk.transform(actor_fn))
        self.params_actor = self.actor.init(next(self.rng), self.fake_state)
        opt_init, self.opt_actor = optix.adam(lr_actor)
        self.opt_state_actor = opt_init(self.params_actor)

        # Entropy coefficient.
        self.target_entropy = -float(action_space.shape[0])
        self.log_alpha = jnp.zeros((), dtype=jnp.float32)
        opt_init, self.opt_alpha = optix.adam(lr_alpha)
        self.opt_state_alpha = opt_init(self.log_alpha)

        # Other parameters.
        cum_p = jnp.arange(0, num_quantiles + 1, dtype=jnp.float32) / num_quantiles
        self.cum_p_prime = jnp.expand_dims((cum_p[1:] + cum_p[:-1]) / 2.0, 0)
        self.num_quantiles = num_quantiles
        self.num_quantiles_to_drop = num_quantiles_to_drop

    @partial(jax.jit, static_argnums=0)
    def _select_action(
        self,
        params_actor: hk.Params,
        state: np.ndarray,
    ) -> jnp.ndarray:
        mean, _ = self.actor.apply(params_actor, state)
        return jnp.tanh(mean)

    @partial(jax.jit, static_argnums=0)
    def _explore(
        self,
        params_actor: hk.Params,
        key: jnp.ndarray,
        state: np.ndarray,
    ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        mean, log_std = self.actor.apply(params_actor, state)
        return reparameterize_gaussian_and_tanh(mean, log_std, key, False)

    def update(self, writer=None):
        self.learning_step += 1
        weight, batch = self.buffer.sample(self.batch_size)
        state, action, reward, done, next_state = batch

        # Update critic.
        self.opt_state_critic, self.params_critic, loss_critic = self._update_critic(
            opt_state_critic=self.opt_state_critic,
            params_critic=self.params_critic,
            params_critic_target=self.params_critic_target,
            params_actor=self.params_actor,
            log_alpha=self.log_alpha,
            state=state,
            action=action,
            reward=reward,
            done=done,
            next_state=next_state,
            key=next(self.rng),
        )

        # Update actor.
        self.opt_state_actor, self.params_actor, loss_actor, mean_log_pi = self._update_actor(
            opt_state_actor=self.opt_state_actor,
            params_actor=self.params_actor,
            params_critic=self.params_critic,
            log_alpha=self.log_alpha,
            state=state,
            key=next(self.rng),
        )

        # Update alpha.
        self.opt_state_alpha, self.log_alpha, loss_alpha = self._update_alpha(
            opt_state_alpha=self.opt_state_alpha,
            log_alpha=self.log_alpha,
            mean_log_pi=mean_log_pi,
        )

        # Update target network.
        self.params_critic_target = self._update_target(self.params_critic_target, self.params_critic)

        if writer and self.learning_step % 1000 == 0:
            writer.add_scalar("loss/critic", loss_critic, self.learning_step)
            writer.add_scalar("loss/actor", loss_actor, self.learning_step)
            writer.add_scalar("loss/alpha", loss_alpha, self.learning_step)
            writer.add_scalar("stat/alpha", jnp.exp(self.log_alpha), self.learning_step)
            writer.add_scalar("stat/entropy", -mean_log_pi, self.learning_step)

    @partial(jax.jit, static_argnums=0)
    def _update_critic(
        self,
        opt_state_critic: Any,
        params_critic: hk.Params,
        params_critic_target: hk.Params,
        params_actor: hk.Params,
        log_alpha: jnp.ndarray,
        state: np.ndarray,
        action: np.ndarray,
        reward: np.ndarray,
        done: np.ndarray,
        next_state: np.ndarray,
        key: jnp.ndarray,
    ) -> Tuple[Any, hk.Params, jnp.ndarray, jnp.ndarray]:
        loss_critic, grad_critic = jax.value_and_grad(self._loss_critic)(
            params_critic,
            params_critic_target=params_critic_target,
            params_actor=params_actor,
            log_alpha=log_alpha,
            state=state,
            action=action,
            reward=reward,
            done=done,
            next_state=next_state,
            key=key,
        )
        if self.max_grad_norm is not None:
            grad_critic = clip_gradient_norm(grad_critic, self.max_grad_norm)
        update, opt_state_critic = self.opt_critic(grad_critic, opt_state_critic)
        params_critic = optix.apply_updates(params_critic, update)
        return opt_state_critic, params_critic, loss_critic

    @partial(jax.jit, static_argnums=0)
    def _loss_critic(
        self,
        params_critic: hk.Params,
        params_critic_target: hk.Params,
        params_actor: hk.Params,
        log_alpha: jnp.ndarray,
        state: np.ndarray,
        action: np.ndarray,
        reward: np.ndarray,
        done: np.ndarray,
        next_state: np.ndarray,
        key: jnp.ndarray,
    ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        alpha = jnp.exp(log_alpha)
        # Sample next actions.
        next_mean, next_log_std = self.actor.apply(params_actor, next_state)
        next_action, next_log_pi = reparameterize_gaussian_and_tanh(next_mean, next_log_std, key, True)
        # Calculate target soft quantile values with target critic.
        next_quantile = jnp.concatenate(self.critic.apply(params_critic_target, next_state, next_action), axis=1)
        # Sort the ensemble of quantile values and drop large quantile values.
        next_quantile = jnp.sort(next_quantile, axis=1)[:, : self.num_quantiles - self.num_quantiles_to_drop]
        next_quantile = next_quantile - alpha * next_log_pi
        target_quantile = jax.lax.stop_gradient(reward + (1.0 - done) * self.discount * next_quantile)[:, None, :]
        # Calculate current soft quantile values with online critic.
        curr_quantile_list = self.critic.apply(params_critic, state, action)
        loss = 0.0
        for curr_quantile in curr_quantile_list:
            loss += quantile_loss(target_quantile - curr_quantile[:, :, None], self.cum_p_prime, 1.0, "huber")
        return loss

    @partial(jax.jit, static_argnums=0)
    def _update_actor(
        self,
        opt_state_actor: Any,
        params_actor: hk.Params,
        params_critic: hk.Params,
        log_alpha: jnp.ndarray,
        state: np.ndarray,
        key: jnp.ndarray,
    ) -> Tuple[Any, hk.Params, jnp.ndarray, jnp.ndarray]:
        (loss_actor, mean_log_pi), grad_actor = jax.value_and_grad(self._loss_actor, has_aux=True)(
            params_actor,
            params_critic=params_critic,
            log_alpha=log_alpha,
            state=state,
            key=key,
        )
        if self.max_grad_norm is not None:
            grad_actor = clip_gradient_norm(grad_actor, self.max_grad_norm)
        update, opt_state_actor = self.opt_actor(grad_actor, opt_state_actor)
        params_actor = optix.apply_updates(params_actor, update)
        return opt_state_actor, params_actor, loss_actor, mean_log_pi

    @partial(jax.jit, static_argnums=0)
    def _loss_actor(
        self,
        params_actor: hk.Params,
        params_critic: hk.Params,
        log_alpha: jnp.ndarray,
        state: np.ndarray,
        key: np.ndarray,
    ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        alpha = jnp.exp(log_alpha)
        # Sample actions.
        mean, log_std = self.actor.apply(params_actor, state)
        action, log_pi = reparameterize_gaussian_and_tanh(mean, log_std, key, True)
        # Calculate soft q values with online critic.
        mean_q = jnp.concatenate(self.critic.apply(params_critic, state, action), axis=1).mean()
        mean_log_pi = log_pi.mean()
        return alpha * mean_log_pi - mean_q, jax.lax.stop_gradient(mean_log_pi)

    @partial(jax.jit, static_argnums=0)
    def _update_alpha(
        self,
        opt_state_alpha: Any,
        log_alpha: jnp.ndarray,
        mean_log_pi: jnp.ndarray,
    ) -> Tuple[Any, jnp.ndarray, jnp.ndarray]:
        loss_alpha, grad_alpha = jax.value_and_grad(self._loss_alpha)(
            log_alpha,
            mean_log_pi=mean_log_pi,
        )
        update, opt_state_alpha = self.opt_alpha(grad_alpha, opt_state_alpha)
        log_alpha = optix.apply_updates(log_alpha, update)
        return opt_state_alpha, log_alpha, loss_alpha

    @partial(jax.jit, static_argnums=0)
    def _loss_alpha(
        self,
        log_alpha: jnp.ndarray,
        mean_log_pi: jnp.ndarray,
    ) -> jnp.ndarray:
        return -log_alpha * (self.target_entropy + mean_log_pi)
