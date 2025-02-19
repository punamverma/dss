import base64
import datetime
import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
import uuid

import cryptography.exceptions
import cryptography.hazmat.backends
import cryptography.hazmat.primitives.hashes
import cryptography.hazmat.primitives.serialization
import cryptography.x509
import jwcrypto.common
import jwcrypto.jwk
import jwcrypto.jws
import jwcrypto.jwt
import requests
from google.auth.transport import requests as google_requests
from google.oauth2 import service_account
from monitoring.monitorlib.infrastructure import AuthAdapter


_UNIX_EPOCH = datetime.datetime.utcfromtimestamp(0)


class NoAuth(AuthAdapter):
    """Auth adapter that generates tokens without an auth server.

    While no server is used, the access tokens generated are fully valid and their
    signatures will validate against test-certs/auth2.pem.
    """

    # This is the private key from test-certs/auth2.key.
    dummy_private_key = jwcrypto.jwk.JWK.from_pem(
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIICWwIBAAKBgHkNtpy3GB0YTCl2VCCd22i0rJwIGBSazD4QRKvH6rch0IP4igb+\n"
        "02r7t0X//tuj0VbwtJz3cEICP8OGSqrdTSCGj5Y03Oa2gPkx/0c0V8D0eSXS/CUC\n"
        "0qrYHnAGLqko7eW87HW0rh7nnl2bB4Lu+R8fOmQt5frCJ5eTkzwK5YczAgMBAAEC\n"
        "gYAtSgMjGKEt6XQ9IucQmN6Iiuf1LFYOB2gYZC+88PuQblc7uJWzTk08vlXwG3l3\n"
        "JQ/h7gY0n6JhH8RJW4m96TO8TrlHLx5aVcW8E//CtgayMn3vBgXida3wvIlAXT8G\n"
        "WezsNsWorXLVmz5yov0glu+TIk31iWB5DMs4xXhXdH/t8QJBALQzvF+y5bZEhZin\n"
        "qTXkiKqMsKsJbXjP1Sp/3t52VnYVfbxN3CCb7yDU9kg5QwNa3ungE3cXXNMUr067\n"
        "9zIraekCQQCr+NSeWAXIEutWewPIykYMQilVtiJH4oFfoEpxvecVv7ulw6kM+Jsb\n"
        "o6Pi7x86tMVkwOCzZzy/Uyo/gSHnEZq7AkEAm0hBuU2VuTzOyr8fhvtJ8X2O97QG\n"
        "C6c8j4Tk7lqXIuZeFRga6la091vMZmxBnPB/SpX28BbHvHUEpBpBZ5AVkQJAX7Lq\n"
        "7urg3MPafpeaNYSKkovG4NGoJgSgJgzXIJCjJfE6hTZqvrMh7bGUo9aZtFugdT74\n"
        "TB2pKncnTYuYyDN9vQJACDVr+wvYYA2VdnA9k+/1IyGc1HHd2npQqY9EduCeOGO8\n"
        "rXQedG6rirVOF6ypkefIayc3usipVvfadpqcS5ERhw==\n"
        "-----END RSA PRIVATE KEY-----".encode("UTF-8")
    )

    EXPIRATION = 3600  # seconds

    def __init__(self, sub: str = "uss_noauth"):
        super().__init__()
        self.sub = sub

    # Overrides method in AuthAdapter
    def issue_token(self, intended_audience: str, scopes: List[str]) -> str:
        timestamp = int((datetime.datetime.utcnow() - _UNIX_EPOCH).total_seconds())
        jwt = jwcrypto.jwt.JWT(
            header={"typ": "JWT", "alg": "RS256"},
            claims={
                "sub": self.sub,
                "client_id": self.sub,
                "scope": " ".join(scopes),
                "aud": intended_audience,
                "nbf": timestamp - 1,
                "exp": timestamp + NoAuth.EXPIRATION,
                "iss": "NoAuth",
                "jti": str(uuid.uuid4()),
            },
            algs=["RS256"],
        )
        jwt.make_signed_token(NoAuth.dummy_private_key)
        return jwt.serialize()


class DummyOAuth(AuthAdapter):
    """Auth adapter that gets JWTs that uses the Dummy OAuth Server"""

    def __init__(self, token_endpoint: str, sub: str):
        super().__init__()

        self._oauth_token_endpoint = token_endpoint
        self._sub = sub
        self._oauth_session = requests.Session()

    # Overrides method in AuthAdapter
    def issue_token(self, intended_audience: str, scopes: List[str]) -> str:
        url = "{}?grant_type=client_credentials&scope={}&intended_audience={}&issuer=dummy&sub={}".format(
            self._oauth_token_endpoint,
            urllib.parse.quote(" ".join(scopes)),
            urllib.parse.quote(intended_audience),
            self._sub,
        )
        response = self._oauth_session.post(url)
        if response.status_code != 200:
            raise AccessTokenError(
                'Request to get DummyOAuth access token returned {} "{}" at {}'.format(
                    response.status_code, response.content.decode("utf-8"), response.url
                )
            )
        return response.json()["access_token"]


class ServiceAccount(AuthAdapter):
    """Auth adapter that gets JWTs using a service account."""

    def __init__(self, token_endpoint: str, service_account_json: str):
        super().__init__()

        credentials = service_account.Credentials.from_service_account_file(
            service_account_json
        ).with_scopes(["email"])
        oauth_session = google_requests.AuthorizedSession(credentials)

        self._oauth_token_endpoint = token_endpoint
        self._oauth_session = oauth_session

    # Overrides method in AuthAdapter
    def issue_token(self, intended_audience: str, scopes: List[str]) -> str:
        url = "{}?grant_type=client_credentials&scope={}&intended_audience={}".format(
            self._oauth_token_endpoint,
            urllib.parse.quote(" ".join(scopes)),
            urllib.parse.quote(intended_audience),
        )
        response = self._oauth_session.post(url)
        if response.status_code != 200:
            raise AccessTokenError(
                'Request to get ServiceAccount access token returned {} "{}" at {}'.format(
                    response.status_code, response.content.decode("utf-8"), response.url
                )
            )
        return response.json()["access_token"]


class UsernamePassword(AuthAdapter):
    """Auth adapter that gets JWTs using a username and password."""

    def __init__(
        self, token_endpoint: str, username: str, password: str, client_id: str
    ):
        super().__init__()

        self._oauth_token_endpoint = token_endpoint
        self._username = username
        self._password = password
        self._client_id = client_id

    # Overrides method in AuthAdapter
    def issue_token(self, intended_audience: str, scopes: List[str]) -> str:
        scopes.append("aud:{}".format(intended_audience))
        response = requests.post(
            self._oauth_token_endpoint,
            data={
                "grant_type": "password",
                "username": self._username,
                "password": self._password,
                "client_id": self._client_id,
                "scope": " ".join(scopes),
            },
        )
        if response.status_code != 200:
            raise AccessTokenError(
                'Request to get UsernamePassword access token returned {} "{}" at {}'.format(
                    response.status_code, response.content.decode("utf-8"), response.url
                )
            )
        return response.json()["access_token"]


def _load_keypair(
    key_path: str, cert_url: str, backend: Any
) -> Tuple[jwcrypto.jwk.JWK, jwcrypto.jwk.JWK]:
    # Retrieve certificate to validate match with private key
    response = requests.get(cert_url)
    assert response.status_code == 200
    if cert_url[-4:].lower() == ".der":
        cert = cryptography.x509.load_der_x509_certificate(response.content, backend)
    elif cert_url[-4:].lower() == ".crt":
        cert = cryptography.x509.load_pem_x509_certificate(response.content, backend)
    else:
        raise AccessTokenError("cert_url must end with .der or .crt")
    cert_public_key = cert.public_key().public_bytes(
        cryptography.hazmat.primitives.serialization.Encoding.PEM,
        cryptography.hazmat.primitives.serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    # Generate public key directly from private key
    with open(key_path, "r") as f:
        key_content = f.read().encode("utf-8")
    if key_path[-4:].lower() == ".key" or key_path[-4:].lower() == ".pem":
        private_key = cryptography.hazmat.primitives.serialization.load_pem_private_key(
            key_content, password=None, backend=backend
        )
        private_key_bytes = key_content
    else:
        raise AccessTokenError("key_path must end with .key or .pem")
    public_key = private_key.public_key().public_bytes(
        cryptography.hazmat.primitives.serialization.Encoding.PEM,
        cryptography.hazmat.primitives.serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    if cert_public_key != public_key:
        raise AccessTokenError(
            "Public key in certificate does not match private key provided"
        )

    private_jwk = jwcrypto.jwk.JWK.from_pem(private_key_bytes)
    public_jwk = jwcrypto.jwk.JWK.from_pem(public_key)
    return private_jwk, public_jwk


def _make_jws(
    token_headers: Dict[str, str],
    payload: str,
    private_jwk: jwcrypto.jwk.JWK,
    public_jwk: jwcrypto.jwk.JWK,
) -> str:
    # Create JWS
    jws = jwcrypto.jws.JWS(payload.encode("utf-8"))
    jws.add_signature(
        private_jwk, "RS256", protected=jwcrypto.common.json_encode(token_headers)
    )
    signed = jws.serialize(compact=True)

    # Check JWS
    jws_check = jwcrypto.jws.JWS()
    jws_check.deserialize(signed)
    try:
        jws_check.verify(public_jwk, "RS256")
    except jwcrypto.jws.InvalidJWSSignature:
        raise AccessTokenError(
            "Could not construct a valid cryptographic signature for JWS"
        )

    return signed


def _make_signature(
    payload: str, private_jwk: jwcrypto.jwk.JWK, public_jwk: jwcrypto.jwk.JWK
) -> str:
    signer = jwcrypto.jws.JWA.signing_alg("RS256")
    payload_bytes = payload.encode("utf-8")
    signature = signer.sign(private_jwk, payload_bytes)
    signer.verify(public_jwk, payload_bytes, signature)
    return base64.b64encode(signature).decode("utf-8")


class SignedRequest(AuthAdapter):
    """Auth adapter that gets JWTs by signing its outgoing requests."""

    def __init__(
        self,
        token_endpoint: str,
        client_id: str,
        key_path: str,
        cert_url: str,
        key_id: Optional[str] = None,
        signature_style: str = "UPP2",
    ):
        """Create an AuthAdapter that retrieves tokens via message signing.

        Args:
          token_endpoint: URL of the authorization server's token endpoint.
          client_id: ID of client for which the token is being requested.
          key_path: Path to private key with which to sign the token request.
          cert_url: Publicly-accessible URL of certificate containing the public key
            corresponding to the private key in key_path and signed by an authority
            recognized by the authorization server.
          key_id: If specified, the specific ID to supply in the JWS header.  If not
            specified, defaults to the thumbprint of the certificate's public key.
          signature_style: "UPP2" to use a signature in the style of UPP2, "UFT" to
            use a signature in the style of UFT (UPP2 and UFT are FAA
            demonstrations).
        """
        super().__init__()

        self._token_endpoint = token_endpoint
        self._client_id = client_id
        self._cert_url = cert_url
        self._backend = cryptography.hazmat.backends.default_backend()

        self._signature_style = signature_style
        if signature_style not in ("UPP2", "UFT"):
            raise ValueError(
                "signature_style must be either `UPP2` or `UFT`; found `{}`".format(
                    signature_style
                )
            )

        self._private_jwk, self._public_jwk = _load_keypair(
            key_path, cert_url, self._backend
        )

        # Assign key ID
        if key_id:
            self._kid = key_id
        else:
            self._kid = self._public_jwk.thumbprint()

    # Overrides method in AuthAdapter
    def issue_token(self, intended_audience: str, scopes: List[str]) -> str:
        # Construct request body
        query = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "scope": " ".join(scopes),
            "resource": intended_audience,
            "current_timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        }
        payload = "&".join([k + "=" + v for k, v in query.items()])

        # Generate signature
        token_headers = {
            "typ": "JOSE",
            "alg": "RS256",
            "x5u": self._cert_url,
            "kid": self._kid,
        }

        # Add signature header(s) and associated information
        request_headers: Dict[str, str] = {}
        if self._signature_style == "UPP2":
            signature = _make_jws(
                token_headers, payload, self._private_jwk, self._public_jwk
            )
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
            request_headers["x-utm-message-signature"] = re.sub(
                r"\.[^.]*\.", "..", signature
            )
        elif self._signature_style == "UFT":
            content_digest = base64.b64encode(
                hashlib.sha512(payload.encode("utf-8")).digest()
            ).decode("utf-8")
            path = urllib.parse.urlparse(self._token_endpoint).path
            components = [
                "@method",
                "@path",
                "@query",
                "authorization",
                "content-type",
                "content-digest",
                "x-utm-jws-header",
            ]
            signature_content = {
                "@method": "POST",
                "@path": path,
                "@query": "?",
                "authorization": "",
                "content-type": "application/x-www-form-urlencoded",
                "content-digest": "sha-512=:{}:".format(content_digest),
                "x-utm-jws-header": ", ".join(
                    '{}="{}"'.format(k, v) for k, v in token_headers.items()
                ),
                "@signature-params": "({});created={}".format(
                    " ".join('"{}"'.format(c) for c in components),
                    int(datetime.datetime.utcnow().timestamp()),
                ),
            }
            components.append("@signature-params")
            signature_base = "\n".join(
                '"{}": {}'.format(c, signature_content[c]) for c in components
            )
            signature = _make_signature(
                signature_base, self._private_jwk, self._public_jwk
            )

            for k, v in signature_content.items():
                if k[0] != "@":
                    request_headers[k] = v
            request_headers[
                "x-utm-message-signature"
            ] = "utm-message-signature=:{}:".format(signature)
            request_headers[
                "x-utm-message-signature-input"
            ] = "utm-message-signature={}".format(
                signature_content["@signature-params"]
            )
        else:
            raise ValueError("Invalid signature style")

        # Make token request
        response = requests.post(
            self._token_endpoint, data=payload, headers=request_headers
        )
        if response.status_code != 200:
            raise AccessTokenError(
                'Request to get SignedRequest access token returned {} "{}" at {}'.format(
                    response.status_code, response.content.decode("utf-8"), response.url
                )
            )
        return response.json()["access_token"]


class ClientIdClientSecret(AuthAdapter):
    """Auth adapter that gets JWTs using a client ID and client secret. By default, this will send the request as JSON, you can use send_request_as_data flag to send the request as form data."""

    def __init__(
        self,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
        send_request_as_data: bool = False,
    ):
        super().__init__()

        self._oauth_token_endpoint = token_endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        self._send_request_as_data = send_request_as_data

    # Overrides method in AuthAdapter
    def issue_token(self, intended_audience: str, scopes: List[str]) -> str:
        payload = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "audience": intended_audience,
            "scope": " ".join(scopes),
        }

        if self._send_request_as_data:
            response = requests.post(self._oauth_token_endpoint, data=payload)
        else:
            response = requests.post(self._oauth_token_endpoint, json=payload)
        if response.status_code != 200:
            raise AccessTokenError(
                "Unable to retrieve access token:\n" + response.content.decode("utf-8")
            )
        return response.json()["access_token"]


class FlightPassport(ClientIdClientSecret):
    """Auth adpater for Flight Passport OAUTH server (https://www.github.com/openskies-sh/flight_passport)"""

    def __init__(
        self,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
        send_request_as_data: str = "true",
    ):

        send_request_as_data = send_request_as_data.lower() == "true"

        super(FlightPassport, self).__init__(
            token_endpoint, client_id, client_secret, send_request_as_data
        )

        self._send_request_as_data = send_request_as_data


class AccessTokenError(RuntimeError):
    def __init__(self, msg):
        super(AccessTokenError, self).__init__(msg)


def all_subclasses(cls):
    # Reference: https://stackoverflow.com/questions/3862310/how-to-find-all-the-subclasses-of-a-class-given-its-name
    return set(cls.__subclasses__()).union(
        [s for c in cls.__subclasses__() for s in all_subclasses(c)]
    )


def make_auth_adapter(spec: str) -> AuthAdapter:
    """Make an AuthAdapter according to a string specification.

    Args:
      spec: Specification of adapter in the form
        ADAPTER_NAME([VALUE1[,PARAM2=VALUE2][,...]]) where ADAPTER_NAME is the
        name of a subclass of AuthAdapter and the contents of the parentheses are
        *args-style and **kwargs-style values for the parameters of ADAPTER_NAME's
        __init__, but the values (all strings) do not have any quote-like
        delimiters.

    Returns:
      An instance of the appropriate AuthAdapter subclass according to the
      provided spec.
    """
    m = re.match(r"^\s*([^\s(]+)\s*\(\s*([^)]*)\s*\)\s*$", spec)
    if m is None:
        raise ValueError(
            "Auth adapter specification did not match the pattern `AdapterName(param, param, ...)`"
        )

    adapter_name = m.group(1)
    adapter_classes = {cls.__name__: cls for cls in all_subclasses(AuthAdapter)}
    if adapter_name not in adapter_classes:
        raise ValueError("Auth adapter `%s` does not exist" % adapter_name)
    Adapter = adapter_classes[adapter_name]

    adapter_param_string = m.group(2)
    param_strings = [s.strip() for s in adapter_param_string.split(",")]
    args = []
    kwargs = {}
    for param_string in param_strings:
        if "=" in param_string:
            kv = param_string.split("=")
            if len(kv) != 2:
                raise ValueError(
                    "Auth adapter specification contained a parameter with more than one `=` character"
                )
            kwargs[kv[0].strip()] = kv[1].strip()
        else:
            args.append(param_string)

    return Adapter(*args, **kwargs)
