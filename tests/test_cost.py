from src.normalize import blended_price_per_mtok


def test_blended_cost_matches_spec_example():
    # input = 1.00, output = 2.00 -> (3*1 + 1*2) / 4 = 1.25
    result = blended_price_per_mtok(1.00, 2.00, input_weight=3, output_weight=1)
    assert result == 1.25


def test_blended_cost_default_weights_from_settings():
    result = blended_price_per_mtok(1.00, 2.00)
    assert result == 1.25


def test_blended_cost_alternative_ratio_1_to_1():
    result = blended_price_per_mtok(1.00, 2.00, input_weight=1, output_weight=1)
    assert result == 1.5


def test_blended_cost_returns_none_when_input_missing():
    assert blended_price_per_mtok(None, 2.00) is None


def test_blended_cost_returns_none_when_output_missing():
    assert blended_price_per_mtok(1.00, None) is None
