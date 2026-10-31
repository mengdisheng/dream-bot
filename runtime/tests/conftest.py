import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from personal_bot.config import Config
from personal_bot.service import Service
from personal_bot.store import Store


@pytest.fixture
def configuration(tmp_path):
    private = Ed25519PrivateKey.generate()
    public = tmp_path/"gateway.pem"
    public.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo))
    config = Config(tmp_path/"app",tmp_path/"files","runtime-test","epoch-test",public,str(tmp_path/"worker.sock"),str(tmp_path/"bridge.sock"),31002,31000,False)
    return config,private


@pytest.fixture
def service(configuration):
    store = Store(configuration[0])
    service = Service(store)
    connection = service.connections.save({"provider":"custom","billing_mode":"api","base_url":"https://model.example/v1","protocol":"chat_completions","model_id":"mock","api_key":"test-key-only"})
    with store.tx() as db:
        db.execute("UPDATE model_connections SET capabilities=? WHERE id=?",(json.dumps({k:"passed" for k in ("auth","text","stream","tools")}),connection["id"]))
        store.setmeta("default_agent_connection",connection["id"],db)
    yield service
    store.close()


def assertion(private,method,path,role="owner",**extra):
    data = {"sub":"owner","role":role,"method":method,"path":path,"exp":time.time()+30,"request_id":"test-request","deployment_epoch":"epoch-test",**extra}
    encoded = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(private.sign(encoded.encode())).decode().rstrip("=")
    return encoded+"."+signature
