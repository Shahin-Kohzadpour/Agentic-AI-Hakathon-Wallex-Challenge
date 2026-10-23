import unittest

import numpy as np
import pandas as pd

from .learned_returns import FEATURES, add_label, entry_frames, features, fit_month
from .test_candidates import rows


def training_rows():
    rng=np.random.default_rng(44)
    records=[]
    for symbol in ("BTCUSDT","ETHUSDT","SOLUSDT"):
        for i in range(200*6):
            x=rng.normal(0,.01,len(FEATURES))
            t=i*4*3600
            records.append({**dict(zip(FEATURES,x)),"symbol":symbol,"available_at":t,
                            "label24":float(x[2]*2+.005),"label_end24":t+24*3600})
    return pd.DataFrame(records)


class LearnedChecks(unittest.TestCase):
    def test_future_observations_cannot_change_fitted_model_or_calibration(self):
        f=training_rows()
        cutoff=180*86400
        first,a=fit_month(f,"ridge",24,cutoff)
        changed=f.copy()
        future=changed.available_at>=cutoff
        changed.loc[future,FEATURES]=100000
        changed.loc[future,"label24"]=-100000
        second,b=fit_month(changed,"ridge",24,cutoff)
        np.testing.assert_array_equal(first.predict(f[FEATURES].iloc[:10]),second.predict(f[FEATURES].iloc[:10]))
        self.assertEqual(a,b)
        self.assertLess(a["training_labels_end"],a["calibration_start"])
        self.assertLess(a["calibration_labels_end"],cutoff)

    def test_calibration_labels_cannot_change_training_weights(self):
        f=training_rows()
        cutoff=180*86400
        first,_=fit_month(f,"ridge",24,cutoff)
        changed=f.copy()
        changed.loc[changed.label_end24>=cutoff-30*86400,"label24"]=500
        second,_=fit_month(changed,"ridge",24,cutoff)
        np.testing.assert_array_equal(first.predict(f[FEATURES].iloc[:10]),second.predict(f[FEATURES].iloc[:10]))

    def test_boosting_has_fixed_iterations_and_no_random_validation_split(self):
        model,a=fit_month(training_rows(),"hist_gradient_boosting",24,180*86400)
        self.assertIsNotNone(model)
        self.assertFalse(model.get_params()["early_stopping"])
        self.assertEqual(model.get_params()["max_leaf_nodes"],7)
        self.assertEqual(model.n_iter_,100)

    def test_feature_prefix_is_unchanged_by_future_candles(self):
        frame=pd.DataFrame(rows(1500,3600))
        short=features(frame.iloc[:1200],.005)
        full=features(frame,.005)
        pd.testing.assert_frame_equal(short,full.iloc[:1200])

    def test_labels_use_next_open_and_both_sides_costs(self):
        frame=pd.DataFrame(rows(26,3600))
        frame["o"]=100.
        frame.loc[25,"o"]=110.
        frame["available_at"]=frame.t+3600
        label,end=add_label(frame,24,.001)
        expected=110*.999*.998/(100*1.001*1.002)-1
        self.assertAlmostEqual(label.iloc[0],expected)
        self.assertEqual(end.iloc[0],25*3600)
        broken=frame.drop(index=10).reset_index(drop=True)
        missing,_=add_label(broken,24,.001)
        self.assertTrue(pd.isna(missing.iloc[0]))

    def test_entry_decision_does_not_consult_future_label_availability(self):
        f=pd.DataFrame(rows(200,3600))
        f["available_at"]=f.t+3600
        f["feature_ready"]=True
        f["daily_atr"]=1.
        f["cost_return"]=.005
        f["label24"]=1.
        forecasts=pd.DataFrame({"symbol":"BTCUSDT","available_at":f.available_at,
                              "forecast_net":.01,"fit_cutoff":0,"calibration_passed":True})
        before=entry_frames({"BTCUSDT":f},forecasts,24,200*3600)
        f["label24"]=np.nan
        after=entry_frames({"BTCUSDT":f},forecasts,24,200*3600)
        pd.testing.assert_frame_equal(before["BTCUSDT"],after["BTCUSDT"])
