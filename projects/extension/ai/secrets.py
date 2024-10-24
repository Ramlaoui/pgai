import os
from urllib.parse import urljoin

import backoff
import httpx
from backoff._typing import Details

GUC_SECRETS_MANAGER_URL = "ai.external_functions_executor_url"

DEFAULT_SECRETS_MANAGER_PATH = "/api/v1/projects/secrets"


def get_guc_value(plpy, setting: str, default: str) -> str:
    plan = plpy.prepare("select pg_catalog.current_setting($1, true) as val", ["text"])
    result = plan.execute([setting], 1)
    val: str | None = None
    if len(result) != 0:
        val = result[0]["val"]
    if val is None:
        val = default
    return val


def check_secret_permissions(plpy, secret_name: str) -> bool:
    # check if the user has access to all secrets
    plan = plpy.prepare(
        """
                        SELECT 1
                        FROM ai.secret_permissions 
                        WHERE name = '*'""",
        [],
    )
    result = plan.execute([], 1)
    if len(result) > 0:
        return True

    # check if the user has access to the specific secret
    plan = plpy.prepare(
        """
                        SELECT 1
                        FROM ai.secret_permissions 
                        WHERE name = $1 
                        """,
        ["text"],
    )
    result = plan.execute([secret_name], 1)
    return len(result) > 0


def reveal_secret(plpy, secret_name: str) -> str | None:
    # first try the guc, then the secrets manager, then error
    secret_name_lower = secret_name.lower()
    secret = get_guc_value(plpy, f"ai.{secret_name_lower}", "")
    if secret != "":
        return secret

    env_secret = os.environ.get(secret_name.upper())
    if env_secret is not None:
        return env_secret

    if secret_manager_enabled(plpy):
        secret_optional = fetch_secret(plpy, secret_name)
        if secret_optional is not None:
            return secret_optional

    return None


def secret_manager_enabled(plpy) -> bool:
    return get_guc_value(plpy, GUC_SECRETS_MANAGER_URL, "") != ""


def fetch_secret(plpy, secret_name: str) -> str | None:
    if not secret_manager_enabled(plpy):
        plpy.error("secrets manager is not enabled")
        return None

    if not check_secret_permissions(plpy, secret_name):
        plpy.error(f"user does not have access to secret '{secret_name}'")
        return None

    the_url = urljoin(
        get_guc_value(plpy, GUC_SECRETS_MANAGER_URL, ""),
        DEFAULT_SECRETS_MANAGER_PATH,
    )
    plpy.debug(f"executing secret reveal request to {the_url}")

    def on_backoff(detail: Details):
        plpy.warning(
            f"reveal secret '{secret_name}' retry: {detail['tries']} elapsed: {detail['elapsed']} wait: {detail['wait']}..."
        )

    @backoff.on_exception(
        backoff.expo,
        httpx.HTTPError,
        max_tries=3,
        max_time=10,
        on_backoff=on_backoff,
        raise_on_giveup=True,
    )
    def get() -> httpx.Response:
        return httpx.get(the_url, headers={"Secret-Name": secret_name})

    r = get()

    if r.status_code == httpx.codes.NOT_FOUND:
        return None

    if r.status_code != httpx.codes.OK:
        plpy.error(
            f"failed to reveal secret '{secret_name}': {r.status_code}", detail=r.text
        )
    return r.json()[secret_name]
