from scripts.update_tunnel import upsert_ingress

SVC = "http://localhost:8000"


def test_agrega_antes_del_catch_all_sin_tocar_lo_demas():
    config = {"tunnel": "abc", "credentials-file": "/x.json",
              "ingress": [{"hostname": "web.dom.com", "service": "http://localhost:3000"},
                          {"service": "http_status:404"}]}
    upsert_ingress(config, "api.dom.com", SVC)
    assert config["tunnel"] == "abc"
    assert config["ingress"] == [
        {"hostname": "web.dom.com", "service": "http://localhost:3000"},
        {"hostname": "api.dom.com", "service": SVC},
        {"service": "http_status:404"},
    ]


def test_actualiza_si_ya_existe():
    config = {"ingress": [{"hostname": "api.dom.com", "service": "http://localhost:9999"},
                          {"service": "http_status:404"}]}
    upsert_ingress(config, "api.dom.com", SVC)
    assert config["ingress"][0]["service"] == SVC
    assert len(config["ingress"]) == 2


def test_sin_ingress_crea_catch_all():
    config = {"tunnel": "abc"}
    upsert_ingress(config, "api.dom.com", SVC)
    assert config["ingress"] == [{"hostname": "api.dom.com", "service": SVC},
                                 {"service": "http_status:404"}]
