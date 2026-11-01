"""Lecture protégée de config.py (installation à la main, ancien nom APIKEY)."""
import lambda_function as lf


def _write(tmp_path, text):
    path = tmp_path / "config.py"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_valeurs_lues(tmp_path):
    cfg = lf.load_config(_write(tmp_path, 'JEEDOM_URL = "https://j.example/"\nJEEDOM_APIKEY = "abc"\nDEBUG = True\n'))
    assert cfg == {"JEEDOM_URL": "https://j.example/", "JEEDOM_APIKEY": "abc", "DEBUG": True}


def test_faute_de_frappe_sans_exception(tmp_path):
    assert lf.load_config(_write(tmp_path, 'JEEDOM_URL = "https://j.example\n')) == {}


def test_fichier_absent(tmp_path):
    assert lf.load_config(str(tmp_path / "absent.py")) == {}


def test_config_du_depot_lisible():
    cfg = lf.load_config(lf.os.path.join(lf.os.path.dirname(lf.__file__), "config.py"))
    assert set(cfg) == {"JEEDOM_URL", "JEEDOM_APIKEY", "VERIFY_SSL", "DEBUG"}
