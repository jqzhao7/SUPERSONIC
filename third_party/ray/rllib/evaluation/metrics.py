import logging
import numpy as np
import collections

import ray
from ray.rllib.evaluation.rollout_metrics import RolloutMetrics
from ray.rllib.policy.sample_batch import DEFAULT_POLICY_ID
from ray.rllib.offline.off_policy_estimator import OffPolicyEstimate
from ray.rllib.policy.policy import LEARNER_STATS_KEY
from ray.rllib.utils.annotations import DeveloperAPI

logger = logging.getLogger(__name__)


@DeveloperAPI
def get_learner_stats(grad_info):
    """Return optimization stats reported from the policy.

    Example:
        >>> grad_info = worker.learn_on_batch(samples)
        {"td_error": [...], "learner_stats": {"vf_loss": ..., ...}}
        >>> print(get_stats(grad_info))
        {"vf_loss": ..., "policy_loss": ...}
    """

    if LEARNER_STATS_KEY in grad_info:
        return grad_info[LEARNER_STATS_KEY]

    multiagent_stats = {}
    for k, v in grad_info.items():
        if type(v) is dict:
            if LEARNER_STATS_KEY in v:
                multiagent_stats[k] = v[LEARNER_STATS_KEY]

    return multiagent_stats


@DeveloperAPI
def collect_metrics(
    local_worker=None, remote_workers=[], to_be_collected=[], timeout_seconds=180
):
    """Gathers episode metrics from RolloutWorker instances."""

    episodes, to_be_collected = collect_episodes(
        local_worker, remote_workers, to_be_collected, timeout_seconds=timeout_seconds
    )
    metrics = summarize_episodes(episodes, episodes)
    return metrics


@DeveloperAPI
def collect_episodes(
    local_worker=None, remote_workers=[], to_be_collected=[], timeout_seconds=180
):
    """Gathers new episodes metrics tuples from the given evaluators."""

    if remote_workers:
        pending = [
            a.apply.remote(lambda ev: ev.get_metrics()) for a in remote_workers
        ] + to_be_collected
        collected, to_be_collected = ray.wait(
            pending, num_returns=len(pending), timeout=timeout_seconds * 1.0
        )
        if pending and len(collected) == 0:
            logger.warning(
                "WARNING: collected no metrics in {} seconds".format(timeout_seconds)
            )
        metric_lists = ray.get(collected)
    else:
        metric_lists = []

    if local_worker:
        metric_lists.append(local_worker.get_metrics())
    episodes = []
    for metrics in metric_lists:
        episodes.extend(metrics)
    return episodes, to_be_collected


@DeveloperAPI
def summarize_episodes(episodes, new_episodes=None):
    """Summarizes a set of episode metrics tuples.

    Arguments:
        episodes: smoothed set of episodes including historical ones
        new_episodes: just the new episodes in this iteration. This must be
            a subset of `episodes`. If None, assumes all episodes are new.
    """

    if new_episodes is None:
        new_episodes = episodes

    episodes, estimates = _partition(episodes)
    new_episodes, _ = _partition(new_episodes)

    episode_rewards = []
    episode_lengths = []
    policy_rewards = collections.defaultdict(list)
    custom_metrics = collections.defaultdict(list)
    perf_stats = collections.defaultdict(list)
    hist_stats = collections.defaultdict(list)
    for episode in episodes:
        episode_lengths.append(episode.episode_length)
        episode_rewards.append(episode.episode_reward)
        for k, v in episode.custom_metrics.items():
            custom_metrics[k].append(v)
        for k, v in episode.perf_stats.items():
            perf_stats[k].append(v)
        for (_, policy_id), reward in episode.agent_rewards.items():
            if policy_id != DEFAULT_POLICY_ID:
                policy_rewards[policy_id].append(reward)
        for k, v in episode.hist_data.items():
            hist_stats[k] += v
    if episode_rewards:
        min_reward = min(episode_rewards)
        max_reward = max(episode_rewards)
        avg_reward = np.mean(episode_rewards)
    else:
        min_reward = float("nan")
        max_reward = float("nan")
        avg_reward = float("nan")
    if episode_lengths:
        avg_length = np.mean(episode_lengths)
    else:
        avg_length = float("nan")

    # Show as histogram distributions.
    hist_stats["episode_reward"] = episode_rewards
    hist_stats["episode_lengths"] = episode_lengths

    policy_reward_min = {}
    policy_reward_mean = {}
    policy_reward_max = {}
    for policy_id, rewards in policy_rewards.copy().items():
        policy_reward_min[policy_id] = np.min(rewards)
        policy_reward_mean[policy_id] = np.mean(rewards)
        policy_reward_max[policy_id] = np.max(rewards)

        # Show as histogram distributions.
        hist_stats["policy_{}_reward".format(policy_id)] = rewards

    for k, v_list in custom_metrics.copy().items():
        filt = [v for v in v_list if not np.isnan(v)]
        custom_metrics[k + "_mean"] = np.mean(filt)
        if filt:
            custom_metrics[k + "_min"] = np.min(filt)
            custom_metrics[k + "_max"] = np.max(filt)
        else:
            custom_metrics[k + "_min"] = float("nan")
            custom_metrics[k + "_max"] = float("nan")
        del custom_metrics[k]

    for k, v_list in perf_stats.copy().items():
        perf_stats[k] = np.mean(v_list)

    estimators = collections.defaultdict(lambda: collections.defaultdict(list))
    for e in estimates:
        acc = estimators[e.estimator_name]
        for k, v in e.metrics.items():
            acc[k].append(v)
    for name, metrics in estimators.items():
        for k, v_list in metrics.items():
            metrics[k] = np.mean(v_list)
        estimators[name] = dict(metrics)

    return dict(
        episode_reward_max=max_reward,
        episode_reward_min=min_reward,
        episode_reward_mean=avg_reward,
        episode_len_mean=avg_length,
        episodes_this_iter=len(new_episodes),
        policy_reward_min=policy_reward_min,
        policy_reward_max=policy_reward_max,
        policy_reward_mean=policy_reward_mean,
        custom_metrics=dict(custom_metrics),
        hist_stats=dict(hist_stats),
        sampler_perf=dict(perf_stats),
        off_policy_estimator=dict(estimators),
    )


def _partition(episodes):
    """Divides metrics data into true rollouts vs off-policy estimates."""

    rollouts, estimates = [], []
    for e in episodes:
        if isinstance(e, RolloutMetrics):
            rollouts.append(e)
        elif isinstance(e, OffPolicyEstimate):
            estimates.append(e)
        else:
            raise ValueError("Unknown metric type: {}".format(e))
    return rollouts, estimates
