import hashlib
import base64
from loguru import logger

def get_content_digest(payload):
  type_of_payload = str(type(payload))
  if payload:
    payload = payload.strip()
    if type_of_payload != "<class 'bytes'>":
      payload = payload.encode('utf-8')
    if payload == bytes('""', 'utf-8'): #'""' should be treated as an empty payload
      payload = bytes()
  else:
    payload = bytes()
  hash_val = base64.b64encode(hashlib.sha512(payload).digest()).decode('utf-8')
  return hash_val