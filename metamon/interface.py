from functools import lru_cache
import copy
import re
from dataclasses import dataclass, asdict
from typing import Optional, List
from abc import ABC, abstractmethod

import numpy as np
import gymnasium as gym
import string

from poke_env.environment import (
    Battle,
    Move,
    Pokemon,
    Field,
    Effect,
    SideCondition,
    Status,
    PokemonType,
)
from poke_env.player import BattleOrder, Player
from poke_env.data import to_id_str

import metamon
from metamon.tokenizer import PokemonTokenizer, UNKNOWN_TOKEN
from metamon.data.replay_dataset.replay_parser.replay_state import (
    Move as ReplayMove,
    Pokemon as ReplayPokemon,
    Action as ReplayAction,
    ReplayState,
    Nothing as ReplayNothing,
)


def pokemon_name(name: str) -> str:
    return clean_name(name)


def clean_name(name: str) -> str:
    return to_id_str(str(name)).strip()


@lru_cache(2**13)
def clean_no_numbers(name: str) -> str:
    return "".join(char for char in str(name) if char.isalpha()).lower().strip()


def consistent_pokemon_order(pokemon):
    if not pokemon:
        return []
    if isinstance(pokemon[0], Pokemon):
        key = lambda p: clean_name(p.species)
    elif isinstance(pokemon[0], str):
        key = lambda p: clean_name(p)
    elif isinstance(pokemon[0], UniversalPokemon):
        key = lambda p: clean_name(p.name)
    elif isinstance(pokemon[0], ReplayPokemon):
        key = lambda p: clean_name(p.name)
    else:
        raise ValueError(
            f"Unrecognized `pokemon` list format of type {type(pokemon)}: {pokemon}"
        )
    return sorted(pokemon, key=key)


def consistent_move_order(moves):
    if not moves:
        return []
    if isinstance(moves[0], Move):
        key = lambda m: clean_name(m.id)
    elif isinstance(moves[0], str):
        key = lambda m: clean_name(m)
    elif isinstance(moves[0], UniversalMove):
        key = lambda m: clean_name(m.name)
    elif isinstance(moves[0], ReplayMove):
        key = lambda m: clean_name(m.name)
    else:
        raise ValueError(
            f"Unrecognized `moves` list format of type {type(moves[0])}: {moves}"
        )
    return sorted(moves, key=key)


@lru_cache(9)
def hidden_power_reference(gen: int) -> Move:
    # we will map all hidden powers to this move
    return Move(move_id="hiddenpower", gen=gen)


@dataclass
class UniversalMove:
    """An object that represents a move in the backend-agnostic "Universal" format.

    Rarely constructed directly. Instead, use one of the following factory methods:
        - UniversalMove.from_Move(move) - when move is from poke-env
        - UniversalMove.from_ReplayMove(move) - when move is from the replay parser
        - UniversalMove.from_dict(data) - when move is a dict from the parsed replay
            dataset on disk
    """

    name: str
    move_type: str
    category: str
    base_power: int
    accuracy: float
    priority: int
    current_pp: int
    max_pp: int

    @classmethod
    def blank_move(cls):
        return cls(
            name="nomove",
            move_type="nomove",
            category="nomove",
            base_power=0,
            accuracy=1.0,
            priority=0,
            current_pp=0,
            max_pp=0,
        )

    @classmethod
    def from_ReplayMove(cls, move: ReplayMove):
        # ReplayMove overrides Move but has
        # a different pp tracker
        universal_move = cls.from_Move(move)
        if move is not None:
            universal_move.current_pp = move.pp
            universal_move.max_pp = move.maximum_pp
        return universal_move

    @classmethod
    def from_Move(cls, move: Move):
        if move is None:
            return cls.blank_move()
        assert isinstance(move, Move)
        if move.id.startswith("hiddenpower"):
            # we map every hidden power to the typeless version
            # because the types don't show up in replays
            reference = hidden_power_reference(move._gen)
        else:
            reference = move
        return cls(
            name=reference.id,
            category=reference.category.name,
            base_power=reference.base_power,
            move_type=reference.type.name,
            priority=reference.priority,
            accuracy=reference.accuracy,
            # always use `move` for pp tracking
            current_pp=move.current_pp,
            max_pp=move.max_pp,
        )


@dataclass
class UniversalPokemon:
    """An object that represents a pokemon in the backend-agnostic "Universal" format.

    Rarely constructed directly. Instead, use one of the following factory methods:
        - UniversalPokemon.from_ReplayPokemon(pokemon) - when pokemon is from the replay
            parser
        - UniversalPokemon.from_Pokemon(pokemon) - when pokemon is from poke-env
        - UniversalPokemon.from_dict(data) - when pokemon is a dict from the parsed
            replay dataset on disk
    """

    name: str
    hp_pct: float
    types: str
    item: str
    ability: str
    lvl: int
    status: str
    effect: str
    moves: list[UniversalMove]

    atk_boost: int
    spa_boost: int
    def_boost: int
    spd_boost: int
    spe_boost: int
    accuracy_boost: int
    evasion_boost: int

    base_atk: int
    base_spa: int
    base_def: int
    base_spd: int
    base_spe: int
    base_hp: int

    @classmethod
    def from_dict(cls, data: dict):
        data["moves"] = [UniversalMove(**m) for m in data["moves"]]
        return cls(**data)

    @staticmethod
    def universal_items(item_rep: Optional[str | ReplayNothing]) -> str:
        if item_rep is None or item_rep == "unknown_item":
            item_str = "unknownitem"
        elif item_rep == ReplayNothing.NO_ITEM:
            item_str = item_rep.name
        elif isinstance(item_rep, str) and item_rep.strip() in {
            "",
            "No Item",
            "noitem",
        }:
            item_str = ReplayNothing.NO_ITEM.name
        else:
            item_str = item_rep
        return clean_no_numbers(item_str)

    @staticmethod
    def universal_abilities(ability_rep: Optional[str | ReplayNothing]) -> str:
        if ability_rep is None or ability_rep == "unknown_ability":
            ability_str = "unknownability"
        elif ability_rep == ReplayNothing.NO_ABILITY:
            ability_str = ability_rep.name
        elif isinstance(ability_rep, str) and ability_rep.strip() in {
            "",
            "No Ability",
            "noability",
        }:
            ability_str = ReplayNothing.NO_ABILITY.name
        else:
            ability_str = ability_rep
        return clean_no_numbers(ability_str)

    @staticmethod
    def universal_effects(effect_rep: dict[Effect, int]) -> str:
        if not effect_rep:
            return "noeffect"
        most_recent = min(effect_rep.keys(), key=effect_rep.get)
        assert isinstance(most_recent, Effect)
        # get rid of poke-env's effect timing system (Effect.FALLEN5, ... Effect.FALLEN1)
        return clean_no_numbers(most_recent.name)

    @staticmethod
    def universal_status(status_rep: Status | ReplayNothing) -> str:
        if status_rep is None or status_rep == ReplayNothing.NO_STATUS:
            return "nostatus"
        assert isinstance(status_rep, Status)
        return clean_no_numbers(status_rep.name)

    @staticmethod
    def universal_types(type_rep: list) -> str:
        while len(type_rep) < 2:
            type_rep.append(None)

        type_strs = []
        for type in type_rep:
            if type is None:
                type_strs.append("notype")
            elif isinstance(type, PokemonType):
                type_strs.append(clean_name(type.name))
            elif isinstance(type, str):
                type_strs.append(clean_name(type))

        assert len(type_strs) == 2
        return " ".join(sorted(type_strs))

    @classmethod
    def from_ReplayPokemon(cls, pokemon: ReplayPokemon):
        assert isinstance(pokemon, ReplayPokemon)
        moves = [
            UniversalMove.from_ReplayMove(move)
            for move in pokemon.moves.values()
            if move is not None
        ]
        stats = {f"base_{stat}": val for stat, val in pokemon.base_stats.items()}
        boosts = {
            f"{stat}boost": getattr(pokemon.boosts, stat)
            for stat in pokemon.boosts.stat_attrs
        }

        item = cls.universal_items(pokemon.active_item)
        ability = cls.universal_abilities(pokemon.active_ability)
        status = cls.universal_status(pokemon.status)
        effect = cls.universal_effects(pokemon.effects)
        types = cls.universal_types(pokemon.type)
        name = clean_name(pokemon.had_name)

        return cls(
            name=name,
            hp_pct=float(pokemon.current_hp) / pokemon.max_hp,
            types=types,
            item=item,
            ability=ability,
            lvl=pokemon.lvl,
            status=status,
            effect=effect,
            moves=moves,
            **(boosts | stats),
        )

    @classmethod
    def from_Pokemon(cls, pokemon: Pokemon):
        moves = [UniversalMove.from_Move(move) for move in pokemon.moves.values()]
        boosts = {f"{stat}_boost": boost for stat, boost in pokemon.boosts.items()}
        stats = {f"base_{stat}": val for stat, val in pokemon.base_stats.items()}
        status = cls.universal_status(pokemon.status)
        effect = cls.universal_effects(pokemon.effects)
        item = cls.universal_items(pokemon.item)
        ability = cls.universal_abilities(pokemon.ability)
        types = cls.universal_types(pokemon.types)

        return cls(
            name=clean_name(pokemon.species),
            hp_pct=float(pokemon.current_hp_fraction),
            types=types,
            item=item,
            ability=ability,
            lvl=pokemon.level,
            status=status,
            effect=effect,
            moves=moves,
            **(boosts | stats),
        )


@dataclass
class UniversalState:
    """An object that represents a state in the backend-agnostic "Universal" format.

    Rarely constructed directly. Instead, use one of the following factory methods:
        - UniversalState.from_ReplayState(state) - when coming from a ReplayState
            object in the replay parser
        - UniversalState.from_Battle(battle) - when coming from a Battle object in the
            online poke-env
        - UniversalState.from_dict(data) - when state is a dict from the parsed replay
            dataset on disk
    """

    format: str
    player_active_pokemon: UniversalPokemon
    opponent_active_pokemon: UniversalPokemon
    available_switches: List[UniversalPokemon]
    player_prev_move: UniversalMove
    opponent_prev_move: UniversalMove
    opponents_remaining: int
    player_conditions: str
    opponent_conditions: str
    weather: str
    battle_field: str
    forced_switch: bool
    battle_won: bool
    battle_lost: bool

    def __post_init__(self):
        for name, should_be in self.__annotations__.items():
            if should_be in {
                str,
                bool,
                UniversalPokemon,
                UniversalMove,
            } and not isinstance(self.__dict__[name], should_be):
                actually_is = type(self.__dict__[name])
                raise TypeError(f"UniversalState `{name}` has type {actually_is}")

    @staticmethod
    def universal_conditions(condition_rep) -> str:
        if not condition_rep:
            return "noconditions"
        most_recent = max(condition_rep.keys(), key=condition_rep.get)
        assert isinstance(most_recent, SideCondition)
        return clean_no_numbers(most_recent.name)

    @staticmethod
    def universal_field(field_rep) -> str:
        if not field_rep:
            return "nofield"
        most_recent = max(field_rep.keys(), key=field_rep.get)
        assert isinstance(most_recent, Field)
        return clean_no_numbers(most_recent.name)

    @staticmethod
    def universal_weather(weather_rep) -> str:
        if not weather_rep or weather_rep == ReplayNothing.NO_WEATHER:
            return "noweather"
        if isinstance(weather_rep, dict):
            weather_rep = list(weather_rep.keys())[0]
        return clean_no_numbers(weather_rep.name)

    # fmt: off

    @classmethod
    def from_ReplayState(cls, state: ReplayState):
        assert isinstance(state, ReplayState)
        format = re.sub(r"\[|\]| ", "", state.format).lower()
        active = UniversalPokemon.from_ReplayPokemon(state.active_pokemon)
        opponent = UniversalPokemon.from_ReplayPokemon(state.opponent_active_pokemon)
        switches = [UniversalPokemon.from_ReplayPokemon(p) for p in state.available_switches]
        opponents_remaining = 6 - sum(p.status == Status.FNT for p in state.opponent_team if p is not None)
        return cls(
            format=format,
            player_active_pokemon=active,
            opponent_active_pokemon=opponent,
            available_switches=switches,
            player_prev_move=UniversalMove.from_ReplayMove(state.player_prev_move),
            opponent_prev_move=UniversalMove.from_ReplayMove(state.opponent_prev_move),
            player_conditions=cls.universal_conditions(state.player_conditions),
            opponent_conditions=cls.universal_conditions(state.opponent_conditions),
            weather=cls.universal_weather(state.weather),
            battle_field=cls.universal_field(state.battle_field),
            forced_switch=state.force_switch,
            opponents_remaining=opponents_remaining,
            battle_won=state.battle_won,
            battle_lost=state.battle_lost,
        )

    @classmethod
    def from_Battle(cls, battle: Battle):
        format = battle.battle_tag.split("-")[1]
        weather = cls.universal_weather(battle.weather)
        battle_field = cls.universal_field(battle.fields)
        player_conditions = cls.universal_conditions(battle.side_conditions)
        opponent_conditions = cls.universal_conditions(battle.opponent_side_conditions)
        active = UniversalPokemon.from_Pokemon(battle.active_pokemon)
        opponent = UniversalPokemon.from_Pokemon(battle.opponent_active_pokemon)
        possible_switches = [p for p in battle.team.values() if not p.fainted and not p.active]
        switches = [UniversalPokemon.from_Pokemon(p) for p in possible_switches]
        player_prev_move = UniversalMove.from_Move(battle.active_pokemon.previous_move)
        opponent_prev_move = UniversalMove.from_Move(battle.opponent_active_pokemon.previous_move)
        opponents_remaining = 6 - sum(p.status == Status.FNT for p in battle.opponent_team.values())

        force_switch = battle.force_switch
        if isinstance(force_switch, list):
            force_switch = force_switch[0]

        return cls(
            format=format,
            player_active_pokemon=active,
            opponent_active_pokemon=opponent,
            available_switches=switches,
            player_prev_move=player_prev_move,
            opponent_prev_move=opponent_prev_move,
            player_conditions=player_conditions,
            opponent_conditions=opponent_conditions,
            weather=weather,
            battle_field=battle_field,
            forced_switch=force_switch,
            battle_won=battle.won if battle.won else False,
            battle_lost=battle.lost if battle.lost else False,
            opponents_remaining=opponents_remaining,
        )
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict):
        # convert nested Pokemon objects
        data["player_active_pokemon"] = UniversalPokemon.from_dict(data["player_active_pokemon"])
        data["opponent_active_pokemon"] = UniversalPokemon.from_dict(data["opponent_active_pokemon"])
        data["available_switches"] = [UniversalPokemon.from_dict(p) for p in data["available_switches"]]
        # convert nested Move objects
        data["player_prev_move"] = UniversalMove(**data["player_prev_move"])
        data["opponent_prev_move"] = UniversalMove(**data["opponent_prev_move"])
        # create UniversalState from the processed dict
        return cls(**data)


def replaystate_action_to_idx(
    state: ReplayState, action: ReplayAction
) -> Optional[int]:
    """Defines the discrete action space when coming from the replay parser.

    The action space is defined as follows:
        - 0-3: Use the active Pokémon's move 1-4, sorted by alphabetical order
        - 4-8: Switch to available (non-active) Pokémon, sorted by alphabetical order
    """
    # *can* return None, but replay parser will throw an exception if it does.
    action_idx = None
    if action is None or action.is_noop:
        action_idx = -1

    elif action.name == "Struggle":
        action_idx = 0

    elif action.is_switch:
        for switch_idx, available_switch in enumerate(
            consistent_pokemon_order(state.available_switches)
        ):
            if available_switch.unique_id == action.target.unique_id:
                action_idx = 4 + switch_idx
                break
    else:
        move_options = list(state.active_pokemon.moves.values())
        for move_idx, move in enumerate(consistent_move_order(move_options)):
            if move.name == action.name:
                action_idx = move_idx
                break

    return action_idx


def action_idx_to_battle_order(
    battle: Battle, action_idx: int
) -> Optional[BattleOrder]:
    """Defines the discrete action space when coming from the online poke-env env.

    The action space is defined as follows:
        - 0-3: Use the active Pokémon's move 1-4, sorted by alphabetical order
        - 4-8: Switch to available (non-active) Pokémon, sorted by alphabetical order
    """
    if action_idx > 8:
        raise ValueError(
            f"Invalid `action_idx` {action_idx}. The global action space is bounded {0, ..., 8}"
        )

    # we'll only submit an action if it's valid, but we pick from a longer list determined
    # by rules that are easier to keep track of elsewhere
    valid_moves = {m.id for m in battle.available_moves}
    valid_switches = {p.name for p in battle.available_switches}
    move_options = consistent_move_order(list(battle.active_pokemon.moves.values()))
    switch_options = consistent_pokemon_order(
        [p for p in list(battle.team.values()) if not p.fainted and not p.active]
    )

    order = None
    if action_idx <= 3 and not battle.force_switch:
        # pick one of up to 4 available moves
        if action_idx < len(move_options):
            selected_move = move_options[action_idx]
            if selected_move.id in valid_moves:
                order = Player.create_order(selected_move)

    elif 4 <= action_idx <= 8:
        # switch to one of up to 5 alternative pokemon
        action_idx -= 4
        if action_idx < len(switch_options):
            selected_switch = switch_options[action_idx]
            if selected_switch.name in valid_switches:
                order = Player.create_order(selected_switch)

    # Q: "what happens when we pick an invalid action? (order = None)"
    # A : env.ShowdownEnv.on_invalid_order
    return order


class RewardFunction(ABC):
    def __init__(self, *args, **kwargs):
        pass

    def __name__(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    def __call__(self, last_state: UniversalState, state: UniversalState) -> float:
        raise NotImplementedError


class DefaultShapedReward(RewardFunction):
    """The default reward function used by the paper.

    See the Appendix for a full description.
    """

    def __call__(self, last_state: UniversalState, state: UniversalState) -> float:
        active_now = state.player_active_pokemon
        active_prev = None
        for pokemon in [
            last_state.player_active_pokemon,
            *last_state.available_switches,
        ]:
            if pokemon.name == active_now.name:
                active_prev = pokemon
                break
        assert active_prev is not None
        hp_gain = active_now.hp_pct - active_prev.hp_pct
        took_status = float(
            active_now.status != "nostatus" and active_prev.status == "nostatus"
        )
        opp_now = state.opponent_active_pokemon
        opp_prev = last_state.opponent_active_pokemon
        if opp_now.name == opp_prev.name:
            damage_done = opp_prev.hp_pct - opp_now.hp_pct
            gave_status = float(
                opp_now.status != "nostatus" and opp_prev.status == "nostatus"
            )
        else:
            damage_done, gave_status = 0.0, 0.0
        lost_pokemon = float(
            len(last_state.available_switches) > len(state.available_switches)
        )
        removed_pokemon = float(
            last_state.opponents_remaining > state.opponents_remaining
        )
        if state.battle_won:
            victory = 1.0
        elif state.battle_lost:
            victory = -1.0
        else:
            victory = 0.0
        reward = (
            1.0 * (damage_done + hp_gain)
            + 0.5 * (gave_status - took_status)
            + 1.0 * (removed_pokemon - lost_pokemon)
            + 100.0 * victory
        )
        return reward


class BinaryReward(RewardFunction):
    """A sparse variant of the default reward function."""

    def __call__(self, last_state: UniversalState, state: UniversalState) -> float:
        if state.battle_won:
            return 100.0
        elif state.battle_lost:
            return -100.0
        return 0.0


ALL_REWARD_FUNCTIONS = {
    "DefaultShapedReward": DefaultShapedReward,
    "BinaryReward": BinaryReward,
}


class ObservationSpace(ABC):
    def __init__(self, *args, **kwargs):
        self.reset()
        pass

    def __name__(self) -> str:
        return self.__class__.__name__

    def reset(self):
        """Clear any internal state (between battles)."""
        pass

    @property
    def tokenizable(self) -> dict[str, int]:
        """Return a dictionary of tokenizable keys and their expected (max) length."""
        return {}

    @property
    @abstractmethod
    def gym_space(self) -> gym.spaces.Space:
        """Return the observation space for this observation type."""
        raise NotImplementedError

    @abstractmethod
    def state_to_obs(self, state: UniversalState):
        raise NotImplementedError

    def __call__(self, state: UniversalState) -> dict[str, np.ndarray]:
        obs = self.state_to_obs(state)
        return obs


class DefaultObservationSpace(ObservationSpace):
    """The default observation space used by the paper.

    Observations become a dictionary with two keys:
        - "numbers": A 48-dimensional vector of numerical features
        - "text": A string of text features with inconsistent length, but a consistent
            number of whitespace-separated words.
    """

    @property
    def gym_space(self):
        return gym.spaces.Dict(
            {
                "numbers": gym.spaces.Box(
                    low=-10.0,
                    high=10.0,
                    shape=(48,),
                    dtype=np.float32,
                ),
                "text": gym.spaces.Text(
                    max_length=900,
                    min_length=800,
                    charset=set(string.ascii_lowercase)
                    | set(str(n) for n in range(0, 10))
                    | {"<", ">"},
                ),
            }
        )

    @property
    def tokenizable(self) -> dict[str, int]:
        return {
            "text": 87,
        }

    def _get_move_string_features(self, move: UniversalMove, active: bool) -> list[str]:
        out = [clean_name(move.name)]
        if active:
            out += [clean_name(move.move_type), clean_name(move.category)]
        return out

    def _get_move_pad_string(self, active: bool) -> list[str]:
        out = ["<blank>"]
        if active:
            out += ["<blank>", "<blank>"]
        return out

    def _get_move_numerical_features(
        self, move: UniversalMove, active: bool
    ) -> list[float]:
        if not active:
            return []
        # notably missing PP, which (for now) is too unreliable across replay parser vs. poke-env vs. actual pokemon showdown
        return [move.base_power / 200.0, move.accuracy, move.priority / 5.0]

    def _get_move_pad_numerical(self, active: bool) -> list[float]:
        if not active:
            return []
        return [-2.0] * 3

    def _get_pokemon_string_features(
        self, pokemon: UniversalPokemon, active: bool
    ) -> list[str]:
        out = [pokemon.name, pokemon.item, pokemon.ability]
        if active:
            out += [pokemon.types, pokemon.effect, pokemon.status]
        else:
            out += ["<moveset>"]
            move_num = -1
            for move_num, move in enumerate(consistent_move_order(pokemon.moves)):
                out += self._get_move_string_features(move, active=False)
            while move_num < 3:
                out += self._get_move_pad_string(active=False)
                move_num += 1
        return out

    def _get_pokemon_pad_string(self, active: bool) -> list[str]:
        blanks = 3 + (4 if active else 5)
        return ["<blank>"] * blanks

    def _get_pokemon_numerical_features(
        self, pokemon: UniversalPokemon, active: bool
    ) -> list[float]:
        out = [pokemon.hp_pct]
        if active:
            stat = lambda s: getattr(pokemon, f"base_{s}") / 255.0
            boost = lambda b: getattr(pokemon, f"{b}_boost") / 6.0
            out.append(pokemon.lvl / 100.0)
            out += map(stat, ["atk", "spa", "def", "spd", "spe", "hp"])
            out += map(
                boost, ["atk", "spa", "def", "spd", "spe", "accuracy", "evasion"]
            )
        return out

    def _get_pokemon_pad_numerical(self, active: bool) -> list[float]:
        blanks = 1 + (14 if active else 0)
        return [-2.0] * blanks

    def state_to_obs(self, state: UniversalState) -> dict[str, np.ndarray]:
        player_str = ["<player>"] + self._get_pokemon_string_features(
            state.player_active_pokemon, active=True
        )
        numerical = [
            state.opponents_remaining / 6.0
        ] + self._get_pokemon_numerical_features(
            state.player_active_pokemon, active=True
        )

        # consistent move order
        move_str, move_num = [], -1
        for move_num, move in enumerate(
            consistent_move_order(state.player_active_pokemon.moves)
        ):
            move_str += ["<move>"] + self._get_move_string_features(move, active=True)
            numerical += self._get_move_numerical_features(move, active=True)

        while move_num < 3:
            move_str += ["<move>"] + self._get_move_pad_string(active=True)
            numerical += self._get_move_pad_numerical(active=True)
            move_num += 1

        # consistent switch order
        switch_str, switch_num = [], -1
        for switch_num, switch in enumerate(
            consistent_pokemon_order(state.available_switches)
        ):
            switch_str += ["<switch>"] + self._get_pokemon_string_features(
                switch, active=False
            )
            numerical += self._get_pokemon_numerical_features(switch, active=False)
        while switch_num < 4:
            switch_str += ["<switch>"] + self._get_pokemon_pad_string(active=False)
            numerical += self._get_pokemon_pad_numerical(active=False)
            switch_num += 1

        force_switch = "<forcedswitch>" if state.forced_switch else "<anychoice>"
        opponent_str = ["<opponent>"] + self._get_pokemon_string_features(
            state.opponent_active_pokemon, active=True
        )
        numerical += self._get_pokemon_numerical_features(
            state.opponent_active_pokemon, active=True
        )
        global_str = ["<conditions>"] + [
            state.weather,
            state.player_conditions,
            state.opponent_conditions,
        ]
        prev_move_str = (
            ["<player_prev>"]
            + self._get_move_string_features(state.player_prev_move, active=False)
            + ["<opp_prev>"]
            + self._get_move_string_features(state.opponent_prev_move, active=False)
        )
        full_text_list = (
            [f"<{state.format}>", force_switch]
            + player_str
            + move_str
            + switch_str
            + opponent_str
            + global_str
            + prev_move_str
        )
        # length should be 85 (type features have 2 words --> final word length of 87)
        text = " ".join(full_text_list)
        text = np.array(text, dtype=np.str_)
        numbers = np.array(numerical, dtype=np.float32)
        return {"text": text, "numbers": numbers}


class DefaultPlusObservationSpace(DefaultObservationSpace):
    """Adds PP, the opponent's revealed party, and edge case sleep/freeze flags to DefaultObservationSpace.

    The DefaultObservationSpace used by the paper makes Pokémon more long-term-memory-intensive
    than it strictly needs to be:

    1. Sleep/freeze clause relies on remembering our move and the opponent active Pokémon's status
        at previous timesteps.

    2. PP counts can only be inferred by recalling prev_move features at previous timesteps.

    3. The opponent's full team must be inferred from recalling the active Pokémon at previous
        timesteps.

    This observation space moves some of that information into every timestep.
    """

    def reset(self):
        # reset the history-dependent features at the start of each battle
        self.any_opponent_asleep = False
        self.any_opponent_frozen = False
        self.revealed_opponents = set()

    @property
    def gym_space(self):
        base_space = super().gym_space
        base_space["numbers"] = gym.spaces.Box(
            low=-10.0,
            high=10.0,
            # adds 4 PP features + 2 sleep/freeze flags
            shape=(48 + 6,),
            dtype=np.float32,
        )
        return base_space

    @property
    def tokenizable(self) -> dict[str, int]:
        # adds 6 new tokens for the revealed party
        return {
            "text": 87 + 6,
        }

    def _get_move_numerical_features(
        self, move: UniversalMove, active: np.bool
    ) -> list[float]:
        out = super()._get_move_numerical_features(move, active)
        if active:
            pp_ratio = move.current_pp / move.max_pp
            # there's a reason the original obs space doesn't have PP counts ---
            # they are not accurate in replays. Compromise by discretizing to
            # "low pp" warnings that would minimize off-by-one shift:
            pp_warning = (pp_ratio >= 0.5) + (pp_ratio >= 0.25) + (pp_ratio > 0)
            out.append(float(pp_warning))
        return out

    def _get_move_pad_numerical(self, active: bool) -> list[float]:
        if not active:
            return []
        return [-2.0] * 4

    def state_to_obs(self, state: UniversalState):
        # get default observation + PP features
        obs = super().state_to_obs(state)

        opponent = state.opponent_active_pokemon
        # (sleep/freeze clause only activates when *we* put the opponent to sleep/freeze,
        # which is not what's being tracked here, but this covers the main failure case
        # and the subtlety has been learnable without this feature.)
        self.any_opponent_asleep |= opponent.status == "slp"
        self.any_opponent_frozen |= opponent.status == "frz"
        new_features = [
            self.any_opponent_asleep,
            self.any_opponent_frozen,
        ]
        obs["numbers"] = np.concatenate([obs["numbers"], new_features])

        # add a list of revealed opponents padded to length 6 while reusing
        # the existing <blank> token to avoid making a new vocabulary.
        self.revealed_opponents.add(opponent.name)
        revealed = [opp_name for opp_name in sorted(self.revealed_opponents)]
        while len(revealed) < 6:
            revealed.append("<blank>")
        obs["text"] = np.array(
            obs["text"].item() + " " + " ".join(revealed[:6]), dtype=np.str_
        )
        return obs


ALL_OBSERVATION_SPACES = {
    "DefaultObservationSpace": DefaultObservationSpace,
    "DefaultPlusObservationSpace": DefaultPlusObservationSpace,
}


class TokenizedObservationSpace(ObservationSpace):
    """An observation space that tokenizes specified keys of the default observation space.

    Splits text into whitespace-separated words and runs them through a simple
    vocabulary lookup, which usually has been generated by tracking unique words across
    the entire replay dataset. Useful for turning the text features of the default
    observation space into an array with constant shape.
    """

    def __init__(
        self,
        base_obs_space: ObservationSpace,
        tokenizer: PokemonTokenizer,
    ):
        self.base_obs_space = base_obs_space
        self.tokenizer = tokenizer

    def reset(self):
        self.base_obs_space.reset()

    @property
    def gym_space(self):
        tokenizable = self.base_obs_space.tokenizable
        base_space = copy.deepcopy(self.base_obs_space.gym_space)
        new_space_dict = {
            key: space
            for key, space in base_space.spaces.items()
            if key not in tokenizable
        }
        for tokenizable_key, tokenizable_length in tokenizable.items():
            low_token = min(UNKNOWN_TOKEN, 0)
            high_token = max(UNKNOWN_TOKEN, len(self.tokenizer))
            new_space_dict[f"{tokenizable_key}_tokens"] = gym.spaces.Box(
                low=low_token,
                high=high_token,
                shape=(tokenizable_length,),
                dtype=np.int32,
            )

        return gym.spaces.Dict(new_space_dict)

    def state_to_obs(self, state: UniversalState):
        obs = self.base_obs_space.state_to_obs(state)
        for tokenizable_key in self.base_obs_space.tokenizable.keys():
            base_obs_key = obs.pop(tokenizable_key)
            obs[f"{tokenizable_key}_tokens"] = self.tokenizer.tokenize(
                base_obs_key.tolist()
            )
        return obs
