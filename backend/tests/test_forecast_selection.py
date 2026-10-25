"""Forecast selection regressions; synthetic values never represent real shipments."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.forecast import forecast_series, _baseline


def test_observed_demo_series_uses_lower_error_baseline():
    f = forecast_series([4, 2, 0, 0, 1, 2, 7, 17], horizon=4, season=52)
    assert f['method'] == 'naive_last_value'
    assert [p['mean'] for p in f['forecast']] == [17] * 4
    assert f['backtest']['selected']['mae'] == 10
    assert f['backtest']['selected']['mape_pct'] == 79.8
    assert f['backtest']['model']['mape_pct'] == 87.5
    assert f['validation'] == 'limited_history'
    assert f['interval_calibrated'] is False


@pytest.mark.parametrize('missing', [None, '', float('nan'), float('inf'), True])
def test_missing_values_never_become_zero(missing):
    f = forecast_series([1, 2, 3, missing, 5, 6, 7, 8])
    assert f['status'] == 'incomplete_history'
    assert f['forecast'] == []


def test_explicit_zero_is_a_valid_observation():
    f = forecast_series([0] * 12, horizon=3)
    assert f['status'] == 'ok'
    assert [p['mean'] for p in f['forecast']] == [0, 0, 0]


def test_baseline_uncertainty_uses_its_own_errors():
    f = _baseline([0, 2, 4, 6], 4, None)
    assert f['sigma'] == 2
    assert f['points'][0]['mean'] == 6
    assert f['points'][3]['hi80'] - 6 == pytest.approx(2 * (f['points'][0]['hi80'] - 6))


def test_seasonal_baseline_repeats_last_cycle_and_widens_each_cycle():
    f = _baseline([1, 3, 2, 4, 3, 5], 5, 2)
    assert f['method'] == 'seasonal_naive'
    assert [p['mean'] for p in f['points']] == [3, 5, 3, 5, 3]
    assert f['points'][2]['hi80'] - 3 > f['points'][0]['hi80'] - 3


def test_backtest_respects_requested_horizon():
    f = forecast_series([10 + 2 * i for i in range(20)], horizon=1, season=52)
    assert f['method'] == 'damped_trend'
    assert f['backtest']['horizon'] == 1
    assert f['backtest']['selected']['mae'] <= f['backtest']['baseline']['mae']
