from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.tune_nifty_direction_only import (
    direction_labels,
    direction_metrics,
    expected_calibration_error,
    probability_matrix,
)


class _ReorderedProbabilityModel:
    classes_ = np.asarray([2, 0, 1])

    def predict_proba(self, features):
        return np.asarray([[0.70, 0.10, 0.20] for _ in range(len(features))])


def test_direction_labels_use_strict_threshold_and_three_classes():
    returns = np.asarray([-0.004, -0.0025, 0.0, 0.0025, 0.004])
    assert direction_labels(returns, 0.0025).tolist() == [0, 1, 1, 1, 2]


def test_direction_labels_reject_invalid_threshold_and_non_finite_values():
    with pytest.raises(ValueError):
        direction_labels(np.asarray([0.01]), 0.0)
    with pytest.raises(ValueError):
        direction_labels(np.asarray([np.nan]), 0.0025)


def test_probability_matrix_aligns_classes_to_bearish_neutral_bullish_order():
    model = _ReorderedProbabilityModel()
    result = probability_matrix(model, pd.DataFrame({"x": [1.0]}))
    assert result.shape == (1, 3)
    assert result[0].tolist() == pytest.approx([0.10, 0.20, 0.70])


def test_direction_metrics_report_class_recall_and_probability_scores():
    y = np.asarray([0, 1, 2, 0, 1, 2])
    probabilities = np.asarray([
        [0.8, 0.1, 0.1],
        [0.1, 0.8, 0.1],
        [0.1, 0.1, 0.8],
        [0.7, 0.2, 0.1],
        [0.2, 0.7, 0.1],
        [0.1, 0.2, 0.7],
    ])
    result = direction_metrics(y, probabilities)
    assert result["macro_f1"] == pytest.approx(1.0)
    assert result["balanced_accuracy"] == pytest.approx(1.0)
    assert result["class_recall"] == {"bearish": 1.0, "neutral": 1.0, "bullish": 1.0}
    assert result["multiclass_brier"] >= 0.0
    assert result["log_loss"] > 0.0
    assert result["expected_calibration_error"] >= 0.0


def test_expected_calibration_error_is_zero_for_perfect_confidence_predictions():
    y = np.asarray([0, 1, 2])
    probabilities = np.eye(3)
    assert expected_calibration_error(y, probabilities) == pytest.approx(0.0)
