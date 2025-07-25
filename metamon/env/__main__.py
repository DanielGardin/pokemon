import time
from argparse import ArgumentParser

from metamon.baselines.heuristic.basic import GymLeader
from metamon.baselines.model_based.bcrnn_baselines import BaseRNN
from metamon.interface import (
    TokenizedObservationSpace,
    DefaultPlusObservationSpace,
    DefaultShapedReward,
)
from metamon.tokenizer import get_tokenizer
from metamon.env.wrappers import get_metamon_teams, BattleAgainstBaseline


if __name__ == "__main__":

    parser = ArgumentParser()
    parser.add_argument("--battle_format", type=str, default="gen1ou")
    parser.add_argument("--episodes", type=int, default=10)
    parser.add_argument("--team_set", type=str, default="paper_replays")
    parser.add_argument(
        "--battle_backend",
        type=str,
        default="poke-env",
        choices=["poke-env", "metamon"],
    )
    args = parser.parse_args()

    env = BattleAgainstBaseline(
        battle_format=args.battle_format,
        team_set=get_metamon_teams(args.battle_format, args.team_set),
        opponent_type=BaseRNN,
        observation_space=TokenizedObservationSpace(
            DefaultPlusObservationSpace(),
            tokenizer=get_tokenizer("DefaultObservationSpace-v0"),
        ),
        reward_function=DefaultShapedReward(),
        battle_backend=args.battle_backend,
    )

    start = time.time()
    counter = 0
    for ep in range(args.episodes):
        print(f"Episode {ep}")
        inner_start = time.time()
        state, info = env.reset()
        done = False
        return_ = 0.0
        timesteps = 0
        while not done:
            env.render()
            state, reward, terminated, truncated, info = env.step(
                env.action_space.sample()
            )
            return_ += reward
            done = terminated or truncated
            timesteps += 1
            counter += 1
        print(
            f"Episode {ep}:: Timesteps: {timesteps}, Total Return: {return_ : .2f}, FPS: {timesteps / (time.time() - inner_start) : .2f}, Invalid Action: {info['invalid_action_count']}, Valid Actions: {info['valid_action_count']}"
        )

    end = time.time()
    print(f"{counter / (end - start) : .2f} Steps Per Second")
