import unittest

import numpy as np
import pandas as pd

from .adaptive_returns import fit_week
from .learned_returns import FEATURES
from .test_learned_returns import training_rows


class AdaptiveChecks(unittest.TestCase):
    def test_weekly_training_and_calibration_exclude_future_targets(self):
        f=training_rows()
        duplicate=f.copy()
        duplicate["symbol"]=duplicate.symbol.map({"BTCUSDT":"BNBUSDT","ETHUSDT":"XRPUSDT","SOLUSDT":"LINKUSDT"})
        f=pd.concat([f,duplicate],ignore_index=True)
        f["label48"]=f.label24
        f["label_end48"]=f.available_at+48*3600
        cutoff=180*86400
        first,a=fit_week(f,"ridge",cutoff)
        changed=f.copy()
        changed.loc[changed.available_at>=cutoff,FEATURES]=10000
        changed.loc[changed.available_at>=cutoff,"label48"]=-10000
        second,b=fit_week(changed,"ridge",cutoff)
        self.assertIsNotNone(first)
        np.testing.assert_array_equal(first.predict(f[FEATURES].iloc[:10]),second.predict(f[FEATURES].iloc[:10]))
        self.assertEqual(a,b)
        self.assertLess(a["training_labels_end"],cutoff-14*86400)
        self.assertLess(a["calibration_labels_end"],cutoff)
