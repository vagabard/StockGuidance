import numpy as np
from datetime import date
from trainer.dataset.time_training_instance import TimeTrainingInstance

def test_time_training_instance():
    # 3 days, 2 features per day
    num_days = 3
    features_per_day = 2
    feature_vector = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])

    instance = TimeTrainingInstance(
        feature_vector=feature_vector,
        date=date(2023, 1, 1),
        date_id=10,
        training_label=0.5,
        num_days=num_days
    )

    # Test Day 0
    day0 = instance.getFeatureVectorForRelativeDay(0)
    print(f"Day 0: {day0}")
    assert np.array_equal(day0, [1.0, 2.0])

    # Test Day 1
    day1 = instance.getFeatureVectorForRelativeDay(1)
    print(f"Day 1: {day1}")
    assert np.array_equal(day1, [3.0, 4.0])

    # Test Day 2
    day2 = instance.getFeatureVectorForRelativeDay(2)
    print(f"Day 2: {day2}")
    assert np.array_equal(day2, [5.0, 6.0])

    # Test Error for negative
    try:
        instance.getFeatureVectorForRelativeDay(-1)
        assert False, "Should have raised ValueError for negative"
    except ValueError as e:
        print(f"Caught expected error for -1: {e}")

    # Test Error for out of range
    try:
        instance.getFeatureVectorForRelativeDay(3)
        assert False, "Should have raised ValueError for out of range"
    except ValueError as e:
        print(f"Caught expected error for 3: {e}")

if __name__ == "__main__":
    test_time_training_instance()
    print("TimeTrainingInstance tests passed!")
