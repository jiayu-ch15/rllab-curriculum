from sandbox.haoran.mddpg.misc.rllab_util import split_paths
from sandbox.haoran.mddpg.misc.data_processing import create_stats_ordered_dict
from sandbox.tuomas.mddpg.algos.online_algorithm import OnlineAlgorithm
from sandbox.rocky.tf.misc.tensor_utils import flatten_tensor_variables
from sandbox.tuomas.mddpg.policies.stochastic_policy import StochasticNNPolicy
from sandbox.tuomas.mddpg.misc.sampler import ParallelSampler

# for debugging
from sandbox.tuomas.mddpg.misc.sim_policy import rollout, rollout_alg

from rllab.misc.overrides import overrides
from rllab.misc import logger
from rllab.misc import special
from rllab.core.serializable import Serializable
from rllab.envs.proxy_env import ProxyEnv
from rllab.core.serializable import Serializable

from collections import OrderedDict
import numpy as np
import tensorflow as tf

TARGET_PREFIX = "target_"


class VDDPG(OnlineAlgorithm, Serializable):
    """
    Variational DDPG with Stein Variational Gradient Descent using stochastic
    net.
    """

    def __init__(
            self,
            env,
            exploration_strategy,
            policy,
            kernel,
            qf,
            q_prior,
            K,
            q_target_type="max",
            qf_learning_rate=1e-3,
            policy_learning_rate=1e-4,
            Q_weight_decay=0.,
            alpha=1.,
            qf_extra_training=0,
            train_critic=True,
            train_actor=True,
            resume=False,
            n_eval_paths=2,
            svgd_target="action",
            **kwargs
    ):
        """
        :param env: Environment
        :param exploration_strategy: ExplorationStrategy
        :param policy: a multiheaded policy
        :param kernel: specifies discrepancy between heads
        :param qf: QFunctions that is Serializable
        :param K: number of policies
        :param q_target_type: how to aggregate targets from multiple heads
        :param qf_learning_rate: Learning rate of the critic
        :param policy_learning_rate: Learning rate of the actor
        :param Q_weight_decay: How much to decay the weights for Q
        :return:
        """
        Serializable.quick_init(self, locals())
        self.kernel = kernel
        self.qf = qf
        self.q_prior = q_prior
        self.prior_coeff = 0.#1.
        self.K = K
        self.q_target_type = q_target_type
        self.critic_lr = qf_learning_rate
        self.critic_weight_decay = Q_weight_decay
        self.actor_learning_rate = policy_learning_rate
        self.alpha = alpha
        self.qf_extra_training = qf_extra_training
        self.train_critic = train_critic
        self.train_actor = train_actor
        self.resume = resume

        self.alpha_placeholder = tf.placeholder(tf.float32,
                                                shape=(),
                                                name='alpha')

        self.prior_coeff_placeholder = tf.placeholder(tf.float32,
                                                      shape=(),
                                                      name='prior_coeff')
        self.svgd_target = svgd_target
        if svgd_target == "pre-action":
            assert policy.output_nonlinearity == tf.nn.tanh
            assert policy.output_scale == 1.

        assert train_actor or train_critic
        #assert isinstance(policy, StochasticNNPolicy)
        #assert isinstance(exploration_strategy, MNNStrategy)

        #if resume:
        #    qf_params = qf.get_param_values()
        #    policy_params = policy.get_param_values()
        super().__init__(env, policy, exploration_strategy, **kwargs)
        #if resume:
        #    qf.set_param_values(qf_params)
        #    policy.set_param_values(policy_params)

        self.eval_sampler = ParallelSampler(self)
        self.n_eval_paths = n_eval_paths

    @overrides
    def _init_tensorflow_ops(self):

        # Useful dimensions.
        Da = self.env.action_space.flat_dim
        K = self.K

        # Initialize variables for get_copy to work
        self.sess.run(tf.global_variables_initializer())

        self.target_policy = self.policy.get_copy(
            scope_name=TARGET_PREFIX + self.policy.scope_name,
        )
        self.dummy_policy = self.policy.get_copy(
            scope_name="dummy_" + self.policy.scope_name,
        )

        self.target_qf = self.qf.get_copy(
            scope_name=TARGET_PREFIX + self.qf.scope_name,
            action_input=self.target_policy.output
        )

        # TH: It's a bit weird to set class attributes (kernel.kappa and
        # kernel.kappa_grads) outside the class. Could we do this somehow
        # differently?
        # Note: need to reshape policy output from N*K x Da to N x K x Da
        if self.svgd_target == "action":
            actions_reshaped = tf.reshape(self.policy.output, (-1, K, Da))
            self.kernel.kappa = self.kernel.get_kappa(actions_reshaped)
            self.kernel.kappa_grads = self.kernel.get_kappa_grads(
                actions_reshaped)
        elif self.svgd_target == "pre-action":
            pre_actions_reshaped = tf.reshape(self.policy.pre_output, (-1, K, Da))
            self.kernel.kappa = self.kernel.get_kappa(pre_actions_reshaped)
            self.kernel.kappa_grads = self.kernel.get_kappa_grads(
                pre_actions_reshaped)
        else:
            raise NotImplementedError

        self.kernel.sess = self.sess
        self.qf.sess = self.sess
        self.policy.sess = self.sess
        self.target_policy.sess = self.sess
        self.dummy_policy.sess = self.sess

        self._init_ops()

        self.sess.run(tf.global_variables_initializer())


    def _init_ops(self):
        self._init_actor_ops()
        self._init_critic_ops()
        self._init_target_ops()

    def _init_actor_ops(self):
        """
        Note: critic is given as an argument so that we can have several critics

        SVGD
        For easy coding, we can run a session first to update the kernel.
            But it means we need to compute the actor outputs twice. A
            benefit is the kernel sizes can be easily adapted. Otherwise
            tensorflow may differentiate through the kernel size as well.
        An alternative is to manually compute the gradient, but within
            one session.
        A third way is to feed gradients w.r.t. actions to tf.gradients by
            specifying grad_ys.
        Need to write a test case.
        """
        if not self.train_actor:
            pass

        all_true_params = self.policy.get_params_internal()
        all_dummy_params = self.dummy_policy.get_params_internal()
        Da = self.env.action_space.flat_dim

        self.critic_with_policy_input = self.qf.get_weight_tied_copy(
            action_input=self.policy.output,
            observation_input=self.policy.observations_placeholder,
        )
        if self.svgd_target == "action":
            if self.q_prior is not None:
                self.prior_with_policy_input = self.q_prior.get_weight_tied_copy(
                    action_input=self.policy.output,
                    observation_input=self.policy.observations_placeholder,
                )
                p = self.prior_coeff_placeholder
                log_p = ((1.0 - p) * self.critic_with_policy_input.output
                    + p * self.prior_with_policy_input.output)
            else:
                log_p = self.critic_with_policy_input.output
            log_p = tf.squeeze(log_p)
            grad_log_p = tf.gradients(log_p, self.policy.output)
            grad_log_p = tf.reshape(grad_log_p, [-1, self.K, 1, Da])  # N x K x 1 x Da

            kappa = tf.expand_dims(
                self.kernel.kappa,
                dim=3,
            )  # N x K x K x 1

            # grad w.r.t. left kernel input
            kappa_grads = self.kernel.kappa_grads  # N x K x K x Da

            # Stein Variational Gradient!
            action_grads = tf.reduce_mean(
                kappa * grad_log_p
                + self.alpha_placeholder * kappa_grads,
                reduction_indices=1,
            ) # N x K x Da

            # The first two dims needs to be flattened to correctly propagate the
            # gradients to the policy network.
            action_grads = tf.reshape(action_grads, (-1, Da))

            # Propagate the grads through the policy net.
            grads = tf.gradients(
                self.policy.output,
                self.policy.get_params_internal(),
                grad_ys=action_grads,
            )
        elif self.svgd_target == "pre-action":
            if self.q_prior is not None:
                self.prior_with_policy_input = self.q_prior.get_weight_tied_copy(
                    action_input=self.policy.output,
                    observation_input=self.policy.observations_placeholder,
                )
                p = self.prior_coeff_placeholder
                log_p = ((1.0 - p) * self.critic_with_policy_input.output
                    + p * self.prior_with_policy_input.output)
            else:
                log_p = self.critic_with_policy_input.output
            log_p = tf.squeeze(log_p) + \
                self.alpha_placeholder * tf.reduce_sum(
                    tf.log(1. - tf.square(self.policy.output)),
                    reduction_indices=1,
                )

            grad_log_p = tf.gradients(log_p, self.policy.pre_output)
            grad_log_p = tf.reshape(grad_log_p, [-1, self.K, 1, Da])  # N x K x 1 x Da

            kappa = tf.expand_dims(
                self.kernel.kappa,
                dim=3,
            )  # N x K x K x 1

            # grad w.r.t. left kernel input
            kappa_grads = self.kernel.kappa_grads  # N x K x K x Da

            # Stein Variational Gradient!
            pre_action_grads = tf.reduce_mean(
                kappa * grad_log_p
                + self.alpha_placeholder * kappa_grads,
                reduction_indices=1,
            ) # N x K x Da

            # The first two dims needs to be flattened to correctly propagate the
            # gradients to the policy network.
            pre_action_grads = tf.reshape(pre_action_grads, (-1, Da))

            # Propagate the grads through the policy net.
            grads = tf.gradients(
                self.policy.pre_output,
                self.policy.get_params_internal(),
                grad_ys=pre_action_grads,
            )
        else:
            raise NotImplementedError

        self.actor_surrogate_loss = tf.reduce_mean(
            - flatten_tensor_variables(all_dummy_params) *
            flatten_tensor_variables(grads)
        )

        self.train_actor_op = [
            tf.train.AdamOptimizer(
                self.actor_learning_rate).minimize(
                self.actor_surrogate_loss,
                var_list=all_dummy_params)
        ]

        self.finalize_actor_op = [
            tf.assign(true_param, dummy_param)
            for true_param, dummy_param in zip(
                all_true_params,
                all_dummy_params,
            )
        ]

    def _init_critic_ops(self):
        if not self.train_critic:
            return

        q_next = tf.reshape(self.target_qf.output, (-1, self.K))  # N x K
        if self.q_target_type == 'mean':
            q_next = tf.reduce_mean(q_next, reduction_indices=1, name='q_next',
                                    keep_dims=True)  # N x 1
        elif self.q_target_type == 'max':
            q_next = tf.reduce_max(q_next, reduction_indices=1, name='q_next',
                                   keep_dims=True)  # N x 1
        else:
            raise NotImplementedError

        self.ys = (
            self.rewards_placeholder + (1 - self.terminals_placeholder) *
            self.discount * q_next
        )  # N

        self.critic_loss = tf.reduce_mean(tf.square(self.ys - self.qf.output))

        self.critic_reg = tf.reduce_sum(
            tf.pack(
                [tf.nn.l2_loss(v)
                 for v in
                 self.qf.get_params_internal(only_regularizable=True)]
            ),
            name='weights_norm'
        )
        self.critic_total_loss = (
            self.critic_loss + self.critic_weight_decay * self.critic_reg)

        self.train_critic_op = tf.train.AdamOptimizer(self.critic_lr).minimize(
            self.critic_total_loss,
            var_list=self.qf.get_params_internal()
        )

    def _init_target_ops(self):

        if self.train_critic:
            # Set target policy
            actor_vars = self.policy.get_params_internal()
            target_actor_vars = self.target_policy.get_params_internal()
            assert len(actor_vars) == len(target_actor_vars)
            self.update_target_actor_op = [
                tf.assign(target, (self.tau * src + (1 - self.tau) * target))
                for target, src in zip(target_actor_vars, actor_vars)]

            # Set target Q-function
            critic_vars = self.qf.get_params_internal()
            target_critic_vars = self.target_qf.get_params_internal()
            self.update_target_critic_op = [
                tf.assign(target, self.tau * src + (1 - self.tau) * target)
                for target, src in zip(target_critic_vars, critic_vars)
            ]

    @overrides
    def _init_training(self):
        super()._init_training()
        self.target_qf.set_param_values(self.qf.get_param_values())
        self.target_policy.set_param_values(self.policy.get_param_values())
        self.dummy_policy.set_param_values(self.policy.get_param_values())

    @overrides
    def _get_training_ops(self):
        train_ops = list()
        if self.train_actor:
            train_ops += self.train_actor_op
        if self.train_critic:
            train_ops += [self.train_critic_op,
                          self.update_target_actor_op,
                          self.update_target_critic_op]

        return train_ops

    def _get_finalize_ops(self):
        return [self.finalize_actor_op]

    @overrides
    def _update_feed_dict(self, rewards, terminals, obs, actions, next_obs):
        feeds = dict()
        if self.train_actor:
            feeds.update(self._actor_feed_dict(obs))
            feeds.update(
                self.kernel.update(self, feeds, multiheaded=False, K=self.K)
            )
        if self.train_critic:
            feeds.update(self._critic_feed_dict(
                rewards, terminals, obs, actions, next_obs
            ))

        return feeds

    def _actor_feed_dict(self, obs):
        # Note that we want K samples for each observation. Therefore we
        # first need to replicate the observations.
        obs = self._replicate_obs(obs, self.K)

        feed = self.policy.get_feed_dict(obs)
        feed[self.critic_with_policy_input.observations_placeholder] = obs
        feed[self.alpha_placeholder] = self.alpha
        feed[self.prior_coeff_placeholder] = self.prior_coeff
        return feed

    def _critic_feed_dict(self, rewards, terminals, obs, actions, next_obs):
        # Again, we'll need to replicate next_obs.
        next_obs = self._replicate_obs(next_obs, self.K)

        feed = self.target_policy.get_feed_dict(next_obs)

        feed.update({
            self.rewards_placeholder: np.expand_dims(rewards, axis=1),
            self.terminals_placeholder: np.expand_dims(terminals, axis=1),
            self.qf.observations_placeholder: obs,
            self.qf.actions_placeholder: actions,
            self.target_qf.observations_placeholder: next_obs
        })

        return feed

    def _replicate_obs(self, obs, K):
        Do = self.env.observation_space.flat_dim

        obs = np.expand_dims(obs, axis=1)  # N x 1 x Do
        obs = np.tile(obs, (1, K, 1))  # N x K x Do
        obs = np.reshape(obs, (-1, Do))  # N*K x Do


        return obs

    @overrides
    def evaluate(self, epoch, train_info):
        logger.log("Collecting samples for evaluation")
        paths = self.eval_sampler.obtain_samples(
            n_paths=self.n_eval_paths,
            max_path_length=self.max_path_length,
        )
        rewards, terminals, obs, actions, next_obs = split_paths(paths)
        feed_dict = self._update_feed_dict(rewards, terminals, obs, actions,
                                           next_obs)

        # rollout_alg(self)
        #import pdb; pdb.set_trace()

        # Compute statistics
        (
            policy_loss,
            qf_loss,
            policy_outputs,
            target_policy_outputs,
            qf_outputs,
            target_qf_outputs,
            ys,
            kappa,  # N x K x K
        ) = self.sess.run(
            [
                self.actor_surrogate_loss,
                self.critic_loss,
                self.policy.output,
                self.target_policy.output,
                self.qf.output,
                self.target_qf.output,
                self.ys,
                self.kernel.kappa,
            ],
            feed_dict=feed_dict)
        average_discounted_return = np.mean(
            [special.discount_return(path["rewards"], self.discount)
             for path in paths]
        )
        returns = np.asarray([sum(path["rewards"]) for path in paths])
        rewards = np.hstack([path["rewards"] for path in paths])
        Da = self.env.action_space.flat_dim
        policy_vars = np.mean(
            np.var(
                policy_outputs.reshape((-1, self.K, Da)),
                axis=1
            ), axis=1
        )
        kappa_sum = np.sum(kappa, axis=1).ravel()

        # Log statistics
        self.last_statistics.update(OrderedDict([
            ('Epoch', epoch),
            # ('PolicySurrogateLoss', policy_loss),
            #HT: why are the policy outputs info helpful?
            # ('PolicyMeanOutput', np.mean(policy_outputs)),
            # ('PolicyStdOutput', np.std(policy_outputs)),
            # ('TargetPolicyMeanOutput', np.mean(target_policy_outputs)),
            # ('TargetPolicyStdOutput', np.std(target_policy_outputs)),
            ('CriticLoss', qf_loss),
            ('AverageDiscountedReturn', average_discounted_return),
        ]))
        # self.last_statistics.update(create_stats_ordered_dict('Ys', ys))
        self.last_statistics.update(create_stats_ordered_dict('QfOutput',
                                                              qf_outputs))
        # self.last_statistics.update(create_stats_ordered_dict('TargetQfOutput',
        #                                                       target_qf_outputs))
        # self.last_statistics.update(create_stats_ordered_dict('Rewards', rewards))
        self.last_statistics.update(create_stats_ordered_dict('returns', returns))
        self.last_statistics.update(
           create_stats_ordered_dict('PolicyVars',policy_vars)
        )
        self.last_statistics.update(
            create_stats_ordered_dict('KappaSum',kappa_sum)
        )

        es_path_returns = train_info["es_path_returns"]
        if len(es_path_returns) == 0 and epoch == 0:
            es_path_returns = [0]
        if len(es_path_returns) > 0:
            # if eval is too often, training may not even have collected a full
            # path
            train_returns = np.asarray(es_path_returns) / self.scale_reward
            self.last_statistics.update(create_stats_ordered_dict(
                'TrainingReturns', train_returns))

        es_path_lengths = train_info["es_path_lengths"]
        if len(es_path_lengths) == 0 and epoch == 0:
            es_path_lengths = [0]
        if len(es_path_lengths) > 0:
            # if eval is too often, training may not even have collected a full
            # path
            self.last_statistics.update(create_stats_ordered_dict(
                'TrainingPathLengths', es_path_lengths))

        true_env = self.env
        while isinstance(true_env,ProxyEnv):
            true_env = true_env._wrapped_env
        if hasattr(true_env, "log_stats"):
            env_stats = true_env.log_stats(self, epoch, paths)
            self.last_statistics.update(env_stats)

        for key, value in self.last_statistics.items():
            logger.record_tabular(key, value)

    def get_epoch_snapshot(self, epoch):
        return dict(
            epoch=epoch,
            # env=self.env,
            # policy=self.policy,
            # es=self.exploration_strategy,
            # qf=self.qf,
            # kernel=self.kernel,
            algo=self,
        )

    def __getstate__(self):
        d = Serializable.__getstate__(self)
        d.update({
            "policy_params": self.policy.get_param_values(),
            "qf_params": self.qf.get_param_values(),
        })
        return d

    def __setstate__(self, d):
        Serializable.__setstate__(self, d)
        self.qf.set_param_values(d["qf_params"])
        self.policy.set_param_values(d["policy_params"])
