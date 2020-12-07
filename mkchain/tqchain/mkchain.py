import argparse
import base64
import os
import pathlib
import random
import string
import subprocess
import sys
from datetime import datetime, timezone

import yaml

from ._version import get_versions

sys.path.insert(0, "tqchain")

__version__ = get_versions()["version"]


my_path = os.path.dirname(os.path.abspath(__file__))

def run_docker(image, entrypoint, *args):
    return subprocess.check_output(
        "docker run --entrypoint %s --rm %s %s" % (entrypoint, image, " ".join(args)),
        stderr=subprocess.STDOUT,
        shell=True,
    )


def gen_key(image):
    keys = run_docker(
        image,
        "sh",
        "-c",
        "'/usr/local/bin/tezos-client --protocol PsDELPH1Kxsx gen keys mykey && /usr/local/bin/tezos-client --protocol PsDELPH1Kxsx show address mykey -S'",
    ).split(b"\n")

    def extract_key(index: int) -> bytes:
        return base64.b64encode(
            keys[index].split(b":")[index].strip().decode("utf-8").encode("ascii")
        )

    return {"public_key": extract_key(1), "secret_key": extract_key(2)}


def get_rpc_auth_container():
    return {
        "name": "rpc-auth",
        "image": (
            "tezos-rpc-auth:dev"
            if "-" in __version__ or "+" in __version__
            else "tqtezos/tezos-k8s-rpc-auth:%s" % __version__
        ),
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"containerPort": 8080}],
        "env": [
            {"name": "TEZOS_RPC_SERVICE", "value": "tezos-bootstrap-node-rpc"},
            {"name": "TEZOS_RPC_SERVICE_PORT", "value": "8732"},
            {"name": "REDIS_HOST", "value": "redis-service"},
            {"name": "REDIS_PORT", "value": "6379"},
        ],
    }


def get_genesis_vanity_chain_id(seed_len=16):
    seed = "".join(
        random.choice(string.ascii_uppercase + string.digits) for _ in range(seed_len)
    )

    FLEXTESA = "registry.gitlab.com/tezos/flextesa:01e3f596-run"
    return (
        run_docker(
            FLEXTESA,
            "flextesa",
            "vani",
            '""',
            "--seed",
            seed,
            "--first",
            "--machine-readable",
            "csv",
        )
        .decode("utf-8")
        .split(",")[1]
    )


CHAIN_CONSTANTS = {
    "number_of_nodes": {
        "help": "number of peers in the cluster",
        "default": 1,
        "type": int,
    },
    "zerotier_network": {"help": "Zerotier network id for external chain access"},
    "zerotier_token": {"help": "Zerotier token for external chain access"},
    "bootstrap_peer": {"help": "peer ip to join"},
    "docker_image": {
        "help": "Version of the Tezos docker image",
        "default": "tezos/tezos:v7-release",
    },
    "rpc_auth": {
        "help": "Should spin up an RPC authentication server",
        "action": "store_true",
        "default": False,
    },
}


def get_args():
    parser = argparse.ArgumentParser(
        description="Generate helm values for use with the tezos-chain helm chart"
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s {version}".format(version=__version__),
    )

    parser.add_argument("chain_name", action="store", help="Name of your chain")

    for k, v in CHAIN_CONSTANTS.items():
        parser.add_argument(*["--" + k.replace("_", "-")], **v)

    return parser.parse_args()


def main():
    args = get_args()

    if args.number_of_nodes < 1:
        print(
            f"Invalid argument --number-of-nodes {args.number_of_nodes}, must be 1 or more"
        )
        exit(1)

    bootstrap_accounts = [
        "baker",
        "bootstrap_account_1",
        "bootstrap_account_2",
        "genesis",
    ]

    zerotier_image = (
        "tezos-zerotier:dev"
        if "-" in __version__ or "+" in __version__
        else "tqtezos/tezos-k8s-zerotier:%s" % __version__
    )

    rpc_auth_image = (
        "tezos-rpc-auth:dev"
        if "-" in __version__ or "+" in __version__
        else "tqtezos/tezos-k8s-rpc-auth:%s" % __version__
    )

    base_constants = {
        "chain_name": args.chain_name,
        "container_images": {
            "zerotier_docker_image": zerotier_image,
            "rpc_auth_image": rpc_auth_image,
            "tezos_docker_image": args.docker_image,
        },
        "genesis": {
            "genesis_chain_id": get_genesis_vanity_chain_id(),
            "bootstrap_timestamp": datetime.utcnow()
            .replace(tzinfo=timezone.utc)
            .isoformat(),
        },
        "zerotier_in_use": bool(args.zerotier_network),
        "rpc_auth": args.rpc_auth,
        "zerotier_config": {
            "zerotier_network": args.zerotier_network,
            "zerotier_token": args.zerotier_token,
        },
        "nodes": [{"bake_for": "baker"}] + [{}] * (args.number_of_nodes - 1),
    }

    accounts = {"secret_key": [], "public_key": []}
    for account in bootstrap_accounts:
        keys = gen_key(args.docker_image)
        for key_type in keys:
            accounts[key_type].append(
                {
                    "name": account,
                    "key": keys[key_type],
                    "private": key_type == "secret_key",
                    "bootstrap": True,
                    "baker": True,
                }
            )

    bootstrap_peers = [args.bootstrap_peer] if args.bootstrap_peer else []

    creation_constants = {
        **base_constants,
        "accounts": accounts["secret_key"],
        "is_invitation": False,
        "bootstrap_peers": bootstrap_peers,
    }
    invitation_constants = {
        **base_constants,
        "accounts": accounts["public_key"],
        "is_invitation": True,
        "bootstrap_peers": bootstrap_peers,
    }
    invitation_constants.pop("rpc_auth")

    generate_values_dir = f"{pathlib.Path(__file__).parent.absolute()}/../generated-values/"
    pathlib.Path(generate_values_dir).mkdir(exist_ok=True)
    with open(f"{generate_values_dir}{args.chain_name}_values.yaml", "w") as yaml_file:
        yaml.dump(creation_constants, yaml_file)
        print(f"Wrote create constants in {args.chain_name}_values.yaml")
    with open(f"{generate_values_dir}{args.chain_name}_invite_values.yaml", "w") as yaml_file:
        print(f"Wrote invitation constants in {args.chain_name}_invite_values.yaml")
        yaml.dump(invitation_constants, yaml_file)


if __name__ == "__main__":
    main()