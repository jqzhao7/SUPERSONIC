from collections import namedtuple
import logging

from ray.rllib.policy.sample_batch import MultiAgentBatch, SampleBatch
from ray.rllib.utils.annotations import DeveloperAPI

logger = logging.getLogger(__name__)

OffPolicyEstimate = namedtuple("OffPolicyEstimate", ["estimator_name", "metrics"])


@DeveloperAPI
class OffPolicyEstimator:
    """Interface for an off policy reward estimator."""

    @DeveloperAPI
    def __init__(self, policy, gamma):
        """Creates an off-policy estimator.

        Arguments:
            policy (Policy): Policy to evaluate.
            gamma (float): Discount of the MDP.
        """
        self.policy = policy
        self.gamma = gamma
        self.new_estimates = []

    @classmethod
    def create(cls, ioctx):
        """Create an off-policy estimator from a IOContext."""
        gamma = ioctx.worker.policy_config["gamma"]
        # Grab a reference to the current model
        keys = list(ioctx.worker.policy_map.keys())
        if len(keys) > 1:
            raise NotImplementedError(
                "Off-policy estimation is not implemented for multi-agent. "
                "You can set `input_evaluation: []` to resolve this."
            )
        policy = ioctx.worker.get_policy(keys[0])
        return cls(policy, gamma)

    @DeveloperAPI
    def estimate(self, batch):
        """Returns an estimate for the given batch of experiences.

        The batch will only contain data from one episode, but it may only be
        a fragment of an episode.
        """
        raise NotImplementedError

    @DeveloperAPI
    def action_prob(self, batch):
        """Returns the probs for the batch actions for the current policy."""

        num_state_inputs = 0
        for k in batch.keys():
            if k.startswith("state_in_"):
                num_state_inputs += 1
        state_keys = ["state_in_{}".format(i) for i in range(num_state_inputs)]
        log_likelihoods = self.policy.compute_log_likelihoods(
            actions=batch[SampleBatch.ACTIONS],
            obs_batch=batch[SampleBatch.CUR_OBS],
            state_batches=[batch[k] for k in state_keys],
            prev_action_batch=batch.data.get(SampleBatch.PREV_ACTIONS),
            prev_reward_batch=batch.data.get(SampleBatch.PREV_REWARDS),
        )
        return log_likelihoods

    @DeveloperAPI
    def process(self, batch):
        self.new_estimates.append(self.estimate(batch))

    @DeveloperAPI
    def check_can_estimate_for(self, batch):
        """Returns whether we can support OPE for this batch."""

        if isinstance(batch, MultiAgentBatch):
            raise ValueError(
                "IS-estimation is not implemented for multi-agent batches. "
                "You can set `input_evaluation: []` to resolve this."
            )

        if "action_prob" not in batch:
            raise ValueError(
                "Off-policy estimation is not possible unless the inputs "
                "include action probabilities (i.e., the policy is stochastic "
                "and emits the 'action_prob' key). For DQN this means using "
                "`exploration_config: {type: 'SoftQ'}`. You can also set "
                "`input_evaluation: []` to disable estimation."
            )

    @DeveloperAPI
    def get_metrics(self):
        """Return a list of new episode metric estimates since the last call.

        Returns:
            list of OffPolicyEstimate objects.
        """
        out = self.new_estimates
        self.new_estimates = []
        return out
