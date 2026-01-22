from __future__ import annotations

import numpy as np
from typing import List, Tuple, Optional, TYPE_CHECKING
from feature_windowing import best_fit_feature_vector, concat_features_current_then_prior
from load_database_rows_into_tensors import fetch_grouped_by_date

if TYPE_CHECKING:
    from StockRecommenderFromModel import StockRecommenderFromModel

class FeatureVectorBuilder:
    """
    Constructs feature vectors for model input using historical stock data.

    This class encapsulates the logic for taking the output of fetch_grouped_by_date
    and building a feature vector for a specific date, including prior days if requested.
    It ensures that tickers are aligned according to the provided list of ticker IDs.
    """

    @classmethod
    def from_db(
        cls,
        rec: StockRecommenderFromModel,
        *,
        dbinfo_file: str = "dbinfo.id",
        start_date_id: Optional[int] = None,
        end_date_id: Optional[int] = None,
        ticker_pool_end_date_id: Optional[int] = None,
    ) -> FeatureVectorBuilder:
        """
        Fetch data from the database and create a FeatureVectorBuilder instance.

        Args:
            rec: The StockRecommenderFromModel instance to get parameters from.
            dbinfo_file: Path to DB info file.
            start_date_id: Optional start date for fetching.
            end_date_id: Optional end date for fetching.
            ticker_pool_end_date_id: Optional date to anchor the ticker pool.

        Returns:
            A FeatureVectorBuilder instance.
        """
        prior_days = int(rec.get_required_previous_days())
        day_change_centered = rec.get_day_change_centered()
        current_day_change = rec.get_current_day_change_used()
        volume_ratio = rec.get_volume_ratio_used()
        vwap_ratio = rec.get_vwap_ratio_used()
        transaction_ratio = rec.get_tranaction_ratio_used()

        dates, X_flat, _Y, db_ticker_ids = fetch_grouped_by_date(
            dbinfo_file=dbinfo_file,
            day_change_centered=day_change_centered,
            current_day_change=current_day_change,
            volume_ratio=volume_ratio,
            vwap_ratio=vwap_ratio,
            tranaction_ratio=transaction_ratio,
            start_date_id=start_date_id,
            end_date_id=end_date_id,
            ticker_pool_end_date_id=ticker_pool_end_date_id,
        )

        if dates.size == 0 or X_flat.size == 0:
            raise RuntimeError(
                "No data returned from fetch_grouped_by_date; cannot build feature vector."
            )

        num_days = prior_days + 1
        model_ticker_ids = rec.ticker_ids
        num_features = rec.get_num_features_per_ticker()

        return cls(
            X_flat,
            dates,
            db_ticker_ids,
            model_ticker_ids,
            num_features=num_features,
            num_days=num_days
        )

    def __init__(
        self,
        X_flat: np.ndarray,
        dates: np.ndarray,
        db_ticker_ids: List[int],
        model_ticker_ids: List[int],
        num_features: int,
        num_days: int
    ):
        """
        Initialize the FeatureVectorBuilder.

        Args:
            X_flat: Flattened per-date feature matrix (D, T_db * F).
            dates: Array of date IDs corresponding to the rows of X_flat.
            db_ticker_ids: List of ticker IDs as returned by fetch_grouped_by_date.
            model_ticker_ids: List of ticker IDs expected by the model.
            num_features: Number of features per ticker per date.
            num_days: Number of days to include in the output vector (1 + num_prior).
        """
        self.dates = dates
        self.num_features = num_features
        self.num_days = num_days
        self.model_ticker_ids = model_ticker_ids

        # Align X_flat to match model_ticker_ids
        if model_ticker_ids and list(model_ticker_ids) != list(db_ticker_ids):
            num_dates = X_flat.shape[0]
            num_model_tickers = len(model_ticker_ids)
            db_ticker_id_to_idx = {tid: i for i, tid in enumerate(db_ticker_ids)}

            # Identify DB features per ticker. fetch_grouped_by_date returns T_db * F_db.
            # Usually num_features == db_num_features.
            db_num_features = X_flat.shape[1] // len(db_ticker_ids) if db_ticker_ids else 0

            X_aligned = np.full((num_dates, num_model_tickers * num_features), np.nan, dtype=X_flat.dtype)

            for i, tid in enumerate(model_ticker_ids):
                if tid in db_ticker_id_to_idx:
                    db_idx = db_ticker_id_to_idx[tid]
                    copy_size = min(num_features, db_num_features)
                    X_aligned[:, i * num_features : i * num_features + copy_size] = X_flat[
                        :, db_idx * db_num_features : db_idx * db_num_features + copy_size
                    ]
            self.X_flat = X_aligned
        else:
            self.X_flat = X_flat

    def build_for_date(
        self,
        date_id: Optional[int] = None,
        expected_input_size: Optional[int] = None,
        prefer_prior_only: bool = False
    ) -> Tuple[np.ndarray, int]:
        """
        Build a feature vector for the given date_id.

        If date_id is None, the latest available date is used.

        Args:
            date_id: The ID of the date to build features for.
            expected_input_size: Optional size check for the resulting vector.
            prefer_prior_only: If True, tries to exclude current day features.

        Returns:
            A tuple of (feature_vector, actual_date_id).
        """
        if date_id is None:
            date_idx = len(self.dates) - 1
        else:
            matches = np.where(self.dates == date_id)[0]
            if matches.size == 0:
                raise ValueError(f"date_id {date_id} not found in provided data.")
            date_idx = int(matches[0])

        actual_date_id = int(self.dates[date_idx])
        num_prior = self.num_days - 1

        if expected_input_size is not None:
            feat_vec = best_fit_feature_vector(
                self.X_flat,
                date_idx,
                num_prior,
                expected_input_size,
                prefer_prior_only=prefer_prior_only
            )
        else:
            feat_vec = concat_features_current_then_prior(self.X_flat, date_idx, num_prior)

        return feat_vec, actual_date_id
