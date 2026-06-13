import sys; sys.path.insert(0, ".")

import pytest
from ai.narrative import _build_effect_context, parse_effect_response, decide_status_effect


class TestBuildEffectContext:
    def test_basic_context(self):
        ctx = _build_effect_context(
            player_action="Рублю мечом",
            player={"name": "Воин", "class": "warrior", "weapon": "Стальной меч"},
            enemies=[{"name": "Гоблин", "hp": 30, "max_hp": 50}],
            weapon_attrs=["melee", "heavy"],
            damage=15,
        )
        assert "Воин" in ctx
        assert "warrior" in ctx
        assert "Стальной меч" in ctx
        assert "Гоблин" in ctx
        assert "Рублю мечом" in ctx
        assert "15" in ctx
        assert "melee" in ctx

    def test_no_weapon(self):
        ctx = _build_effect_context(
            player_action="Бью кулаком",
            player={"name": "Маг", "class": "mage"},
            enemies=[{"name": "Рат", "hp": 10, "max_hp": 20}],
            weapon_attrs=[],
            damage=3,
        )
        assert "Бью кулаком" in ctx
        assert "3" in ctx
        assert "Оружие:" not in ctx

    def test_multiple_enemies(self):
        ctx = _build_effect_context(
            player_action="Атакую",
            player={"name": "Воин", "class": "warrior"},
            enemies=[
                {"name": "Гоблин", "hp": 20, "max_hp": 50},
                {"name": "Орк", "hp": 40, "max_hp": 80},
            ],
            weapon_attrs=["fire"],
            damage=12,
        )
        assert "Гоблин" in ctx
        assert "Орк" in ctx


class TestParseEffectResponse:
    def test_bleed(self):
        assert parse_effect_response("bleed") == "bleed"
        assert parse_effect_response(" bleed ") == "bleed"
        assert parse_effect_response("Bleed") == "bleed"
        assert parse_effect_response("BLEED") == "bleed"

    def test_poison(self):
        assert parse_effect_response("poison") == "poison"
        assert parse_effect_response(" poison ") == "poison"

    def test_stun(self):
        assert parse_effect_response("stun") == "stun"
        assert parse_effect_response("stun\n") == "stun"

    def test_none(self):
        assert parse_effect_response("none") == "none"
        assert parse_effect_response(" none ") == "none"

    def test_junk(self):
        assert parse_effect_response("") == "none"
        assert parse_effect_response("asdf") == "none"
        assert parse_effect_response("bleed and poison") == "bleed"
        assert parse_effect_response("I think stun would be good") == "stun"


class TestDecideStatusEffect:
    async def test_no_nn_returns_none(self):
        import config
        saved = config.NN_API_URL
        config.NN_API_URL = ""
        try:
            result = await decide_status_effect(
                player_action="Атакую",
                player={"name": "Воин", "class": "warrior"},
                enemies=[{"name": "Гоблин", "hp": 50, "max_hp": 50}],
                weapon_attrs=[],
                damage=10,
            )
            assert result == "none"
        finally:
            config.NN_API_URL = saved
