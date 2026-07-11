#!/usr/bin/env python3
"""
Test script for Value Bet Scanner
Tests functionality with mock data
"""

import json
from datetime import datetime
from value_bet_scanner import (
    ValueBet, ValueBetCalculator, OddsPapiClient
)


def test_ev_calculation():
    """Test Expected Value calculation"""
    print("=" * 50)
    print("Testing EV Calculation")
    print("=" * 50)

    calculator = ValueBetCalculator(min_ev_threshold=2.0)

    # Test case: Sharp odds 2.0, soft odds 2.2
    # True probability = 1/2.0 = 0.5
    # EV = (0.5 * 2.2) - 1 = 0.1 = 10%
    ev = calculator.calculate_ev(soft_odds=2.2, sharp_odds=2.0)
    print(f"Sharp: 2.0, Soft: 2.2 -> EV: {ev:.2f}%")
    assert abs(ev - 10.0) < 0.1, f"Expected 10%, got {ev}"

    # Test case: No value
    ev2 = calculator.calculate_ev(soft_odds=2.0, sharp_odds=2.0)
    print(f"Sharp: 2.0, Soft: 2.0 -> EV: {ev2:.2f}%")
    assert abs(ev2) < 0.1, f"Expected ~0%, got {ev2}"

    # Test case: Negative value
    ev3 = calculator.calculate_ev(soft_odds=1.8, sharp_odds=2.0)
    print(f"Sharp: 2.0, Soft: 1.8 -> EV: {ev3:.2f}%")
    assert ev3 < 0, f"Expected negative, got {ev3}"

    print("\nEV calculations: PASSED\n")


def test_value_bet_creation():
    """Test ValueBet dataclass"""
    print("=" * 50)
    print("Testing ValueBet Creation")
    print("=" * 50)

    bet = ValueBet(
        fixture_id="id1000001772221154",
        participant1="Arsenal FC",
        participant2="Coventry City",
        start_time="2026-08-21T19:00:00.000Z",
        tournament_name="premier-league",
        category_name="England",
        market="1X2 (Full Time Result)",
        market_id="101",
        outcome="Home",
        outcome_id="101",
        sharp_bookmaker="median",
        sharp_odds=2.5,
        soft_bookmaker="cashpoint",
        soft_odds=2.72,
        soft_bookmaker_odds={
            "cashpoint": 2.72,
            "unibet": 2.65,
            "betano": 2.60,
        },
        ev_percentage=8.8,
        win_probability=0.4,
        stake_amount=2.5,
        stake_fraction=0.025,
        bankroll=1000.0,
        kelly_fraction=0.025,
        timestamp="2026-07-05 10:28:30"
    )

    bet_dict = bet.to_dict()

    print("ValueBet fields:")
    for key, value in bet_dict.items():
        print(f"  {key}: {value}")

    assert bet_dict['Datum'] == "2026-07-05 10:28:30"
    assert bet_dict['EV %'] == "8.80%"
    assert 'cashpoint:2.72' in bet_dict['Odds overzicht (soft)']

    print("\nValueBet creation: PASSED\n")


def test_find_value_bets():
    """Test finding value bets between bookmakers"""
    print("=" * 50)
    print("Testing Value Bet Detection")
    print("=" * 50)

    calculator = ValueBetCalculator(min_ev_threshold=2.0)

    # Sharp odds (from Pinnacle)
    sharp_odds = {
        '101': 2.0,  # Home
        '102': 3.5,  # Draw
        '103': 3.8   # Away
    }

    # Soft odds (from bet365)
    soft_odds = {
        '101': 2.15,  # Home - has value!
        '102': 3.3,   # Draw - no value
        '103': 4.2    # Away - has value!
    }

    value_bets = calculator.find_value_bet(
        sharp_odds, soft_odds,
        'pinnacle', 'bet365'
    )

    print(f"\nFound {len(value_bets)} value bets:")
    for vb in value_bets:
        print(f"  {vb['outcome_label']}: Sharp {vb['sharp_odds']:.2f} vs Soft {vb['soft_odds']:.2f}")
        print(f"    EV: {vb['ev_percentage']:.2f}%")

    # Should find 2 value bets (Home and Away)
    assert len(value_bets) == 2, f"Expected 2 value bets, found {len(value_bets)}"

    print("\nValue bet detection: PASSED\n")


def test_market_labels():
    """Test market and outcome labels"""
    print("=" * 50)
    print("Testing Market Labels")
    print("=" * 50)

    calculator = ValueBetCalculator()

    print("Outcome labels:")
    for outcome_id, label in calculator.OUTCOME_LABELS.items():
        print(f"  {outcome_id}: {label}")

    print("\nMarket labels:")
    for market_id, label in calculator.MARKET_LABELS.items():
        print(f"  {market_id}: {label}")

    print("\nMarket labels: OK\n")


def test_implied_probability():
    """Test implied probability calculation"""
    print("=" * 50)
    print("Testing Implied Probability")
    print("=" * 50)

    calculator = ValueBetCalculator()

    test_cases = [
        (2.0, 0.5),   # 50% implied
        (1.5, 0.667), # 66.7% implied
        (3.0, 0.333), # 33.3% implied
        (10.0, 0.1),  # 10% implied
    ]

    for odds, expected_prob in test_cases:
        prob = calculator.calculate_implied_probability(odds)
        print(f"Odds {odds:.2f} -> Probability {prob:.3f} (expected {expected_prob:.3f})")
        assert abs(prob - expected_prob) < 0.02

    print("\nImplied probability: PASSED\n")


def run_all_tests():
    """Run all tests"""
    print("\n")
    print("*" * 60)
    print("*  VALUE BET SCANNER - TEST SUITE")
    print("*" * 60)
    print("\n")

    try:
        test_implied_probability()
        test_ev_calculation()
        test_value_bet_creation()
        test_find_value_bets()
        test_market_labels()

        print("\n")
        print("=" * 50)
        print("ALL TESTS PASSED!")
        print("=" * 50)
        print("\n")

    except AssertionError as e:
        print(f"\nTEST FAILED: {e}")
        return False

    return True


if __name__ == '__main__':
    import sys
    success = run_all_tests()
    sys.exit(0 if success else 1)
