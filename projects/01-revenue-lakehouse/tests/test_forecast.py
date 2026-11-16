import subprocess
import sys


def test_lightgbm_trains_where_pyarrow_parquet_and_dataset_are_blocked():
    script = (
        "import sys\n"
        "for name in ('_parquet', 'parquet', '_dataset', 'dataset'):\n"
        "    sys.modules['pyarrow.' + name] = None\n"
        "import lightgbm as lgb\n"
        "import numpy as np\n"
        "x = np.arange(200, dtype=float).reshape(-1, 1)\n"
        "y = (x[:, 0] % 7).astype(float)\n"
        "params = {'objective': 'regression', 'num_leaves': 7, 'min_data_in_leaf': 5,\n"
        "          'seed': 42, 'deterministic': True, 'force_row_wise': True,\n"
        "          'num_threads': 1, 'verbose': -1}\n"
        "model = lgb.train(params, lgb.Dataset(x, y), num_boost_round=20)\n"
        "print(model.predict(x[:3]).shape[0])\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr[-800:]
    assert done.stdout.strip() == "3"
