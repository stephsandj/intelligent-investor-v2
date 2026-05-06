"""
auth.py - JWT authentication module for the Stock Screening SaaS application.
Secrets from env vars: JWT_SECRET (required), JWT_ALGORITHM (default HS256).
Email delivery via: GMAIL_FROM, GMAIL_APP_PASSWORD, APP_BASE_URL.
"""

import subprocess
import sys

# Auto-install dependencies
for _pkg, _import in [("PyJWT", "jwt"), ("bcrypt", "bcrypt")]:
    try:
        __import__(_import)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", _pkg])

import jwt
import bcrypt

import hashlib
import logging
import os
import re
import secrets
import smtplib
import threading
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
from typing import Dict, Optional, Tuple

import time as _time

from flask import Blueprint, g, jsonify, redirect, request

import models

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _jwt_secret() -> str:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET environment variable is not set")
    return secret


JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 14  # 30→14 days — industry standard for financial SaaS

# ---------------------------------------------------------------------------
# Admin session idle tracking (in-memory; restart forces re-login anyway)
# ---------------------------------------------------------------------------
_admin_last_activity: Dict[str, float] = {}
ADMIN_IDLE_TIMEOUT_SECS = 15 * 60  # must match JS INACTIVITY_MS

def clear_admin_session(admin_id: str) -> None:
    """Remove admin idle-tracking entry — call on logout."""
    _admin_last_activity.pop(admin_id, None)

# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def _hash_token(token: str) -> str:
    """SHA-256 hex digest of a JWT string — used for refresh-token rotation checks."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_tokens(user_id: str) -> Dict[str, str]:
    now = datetime.now(tz=timezone.utc)
    access_payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    refresh_payload = {
        "sub": str(user_id),
        "type": "refresh",
        "iat": now,
        "exp": now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
    }
    secret = _jwt_secret()
    return {
        "access_token":  jwt.encode(access_payload,  secret, algorithm=JWT_ALGORITHM),
        "refresh_token": jwt.encode(refresh_payload, secret, algorithm=JWT_ALGORITHM),
    }


def generate_admin_token(admin_id: str) -> str:
    """Generate a long-lived admin access token stored in admin_access cookie."""
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": str(admin_id),
        "type": "admin_access",
        "iat": now,
        "exp": now + timedelta(hours=8),  # 8-hour admin sessions
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Dict:
    return jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

def _app_base_url() -> str:
    return os.environ.get("APP_BASE_URL", "http://localhost:5051").rstrip("/")


# Logo embedded as a base64 data-URI (400×56 px, Pillow LANCZOS, displayed at
# width:200px in email). Inline data-URIs are never blocked by email clients.
# Generated: python3 -c "from PIL import Image; import base64,io; ..."
_LOGO_EMAIL_B64 = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAZAAAAA4CAYAAADTh677AABZSklEQVR4nO19CbxdVXnvt6a99zl3DkkYwigCagCt"
    "ogURCRVFEHFqYmertthabV9ta/u0bZIO2lc6WRwedlDb+mwTFQcmFUkQQZQgoEEUVDCQBDLc3Omcs4c1vN9/rb3P"
    "3efcezOgrQbuBzf33HPO3nuttdf+xv/3fUSHGTnn2I97DIu0SIu0SItEdFgy4yzLzoyiaBkR2XIO+HG1r9ja39Xn"
    "FdXfrx/jar9Z/3FFUVilFD7jWmsupbyDMTYNgcYYq59nkRZpkRZpkX6SyDkn8Ftr/YvWWvfjJq31Lc65AQiQRato"
    "kRZpkZ6MJOkwI8bYhYwxMsbkQghpLYyNQM7NGgL4Duecqs/xuiK8V30X36u/rn+3fu7ybyal9FaPMeYcIcRRjLHv"
    "OedwwKIVskiLtEhPKpKHowcLzJoxxkvG3UMQApUggGCoXlfULyzqr6tjw3GVcKm+A8EiyFrijHmro7UoNBZpkRbp"
    "yUxzGPDhQv2CYD5hUae61TEfLXQeHBJ+ekMp+E6apoft+i3SIi3SIj0ZLZCk5OQWwWuttWf6nHPX536CldAVGpzz"
    "LvfvFzT4G3GV6hwgrS2DgcGYdNXXy1M7fJWIGkmCoSzSIi3SIj056bATIM65W4nodUKICH9L2Z1Cj1SoxzxARVF0"
    "4yJ1N1d5zkrAdN+UUsx73urcnPMvEdHOMoC+6MpapEVapCcdHZboIefcC4hoBIYC/tZae+YvpazmA4aOYLfVWlsp"
    "5ZIsyz4opRx1Je62EiLGGHzOrbVXW2s/IKVU5TGmgvJqrSFNqnPjs5yIvrYI412kRVqkJzMddhYIiDH25UP5vnOu"
    "Ya3VldCoI68457bM7bgvjuMvPI6xLAqPRVqkRXpSkjycc0IOgir30nBdePgP+n5zzkV5XvzA+jgQ+RjM457EIi3S"
    "Ii3SYU6HpQAhYnbBT9hsPKJyLznnqoz1LlVoK6CzyngJLBRTvn8wAmSRFmmRFulJTYedAEG8G3z/ED/3QY9KaFTo"
    "LAiPSoD0B90XaZEWaZEW6QkmQCAcrrv/rfHFuz4fUWeQT0jNRtWAIW71ndGZmrEPFgsdG4SG8Xkd/VBea+3BusUW"
    "aZEWaZEW6XASIG4tcbae7O7rjvrN0W3//LYi1QmRiJKUqKV14azLnzV4d5FeN/ah5JKJv+qrT+XrV8GLZW3IMg85"
    "HpyE6C1fskiLtEiLtEgHR4cF11xbCo/pu89ZHrv238pcP1VZdqxy6fLIZcsTYVc0I36ScPrUOCrePX7jKWfCU7V5"
    "87quVRHqWoXSJKEsCdxWIS+kpMWA+H8DodwMwAmbNm2S+P1kKDxZFdic7+fHPbZFWqQnnQWyjojWE9Fgun0gc86a"
    "IrLGpcSLiFnhiLhzJARRLl3EDVd8+xE4bhVt7p6jQmAJIagOnqoF0ReMqyzS4xcejM0FPCz0/hOFFtF5i/RkocNC"
    "gHRJDhptJojzgltmHWcNJhhyCQ0D0Mq5wtmCmIuSggi1DrvEQqY5Ms6DFdJfFwulTP6np/NEphIBZ8fHx08YHh4+"
    "x1o7Zoz57qOPPnoLYyx9oiZgOufib37zm6NJkngBOTMzQ4ODg1Bc+NKlS/Xw8PD4oc4bwBBvPm8k2nxvL5pw1Upy"
    "bM1Bwc4XaZGe5ALEZs65nKSLyVscKiVnFXmQrjNExhJxQdrGpV9qFRHd7F+FoHlv+ZIgRHyPKP+VH9OsnnBUWRit"
    "Vus1URS9XwixHJafUoqOPfbYW51zv8QYe+iJJETgngP8u9PpvOLEE0+8UmudIbdo2bJl2HvYoZIxtp2IXkREk4cy"
    "9/2hDhdpkX6cdHgJEBFxLpqMXIGShkQaz58hPIYO/J8p4ooTo6KcV9eFVaaCVM0G671Aumd/QjCynyBGepGU8j/R"
    "s6UoCgPhYYxB7bJztdafcs6tOlRGejgQY2xoaGhoOWqvYc51yrIsmp6e7n1zP1Shzsc/M/rySNKZNtOFIW0AJXTG"
    "uiQiMc0bu9//8qmPrn8CuwQPV3KeKQVat65XQV2//omhFBxeAiS3PpJBLifrDAlfFNcGzo84BpdEksNf1TevabI+"
    "WEJkjO2JgdTgvIsWyI+G/OI6594sUTQsz5GgKSE8EGvC31EUPdMY87NSyn8us/+fMC4YY0xRdsw0xhgI08o9iiB6"
    "fkj7bKM3jU3C2Bsbg+YVRDkRuirD2gb7SRy1pu3DqzZ/eON6orQSOP+tE1ykgybWey+ekPflsBIgHSWsM6klp0mQ"
    "JChdDMgqCAHByToW0gjnoSruUTWLAvXVxDosEGmHQ9zDOSfb7fZT8BYKVVbrXuu1gjIwz6QnKCHeZq3lSilW22Oh"
    "guchEyOtO3tpOjN5W2iSJL2yJISNCsdjpTpEdxxWz/GThb7+L0ctO6IBM7RNmXFctEMDPNNkNo+P2nX6mm9BoTis"
    "Sf448zpoFfHNm0OkIvxTo1VkKgm+rjomV8YANgULggmq+hF6La8yGY0lQbZPox0ixjr+YQydBueOBw/8f8c8n0xU"
    "lo3B6hrO+Xi4JQ59VvznVeY/eGxRFIgH0ObNm59Qll8113pvmRoK8HEJEaacJKEEF8oxboUlQy48HVxwioZCtehF"
    "+gkgV1qB9933/KFlD9x5XSN3xwLXwy2LKOGssNYB8rOn/dAvEtHnN2wgseYwBkH8WARIVYaK6n5A4HQPAOMlHjPi"
    "yPzjwerwrihYIJAcjJizXnToeYKOQTv2J/EurCobvYbGekL4JH8CiJcxkI1E9EKjIdGZEcIzVhfHsUyzbCJL0w14"
    "Y9WqVU/oda9bXnWf+CGcgRSLOQlLjmu/1x0EMbardWSpsNrGT0j3yOFMSbpdNJ1b0VDuKMM0CaY84zO4Z8JRK3eD"
    "+N5qOrzpf1yAVM/SvptPPSkp9p3vZgqLXk9WOaFUzGEHxCNJZ0c2du1JF9wz4R+60gRpKiE6KuIO2eQu93F0CA4I"
    "EggF7iBEGAK1rg+FBRivfxjrfaPqUF7OuV54zN1+tv0MwMO4nkhB4B8BwYWFdfqnPM+fJoT4rVJ4+BwcS3a3jOPf"
    "bCTJ95/I+SD1bpjYJqUQeVz7xHFIC4BFQuwDXtqgDWnKtNPyqCUL7t1F+vHQkj2FYJKszY1zmtmMGyQQgFlYpxhn"
    "jcMjifsnSoB4txUj1/ryccfI/NHNkcqPp9hSA4FBnxCYBkQtT2kpm/q8c5dfSvRB3TVBcltj4sHqoLIhIP4L3q35"
    "78tcz0HPswzXi667VEom6KVOyeQWfPjBCOttdulJTLX5Z0T0lk6n+ESnM3Oxc245E+IezfNPjTXGHqziJfQEI0t+"
    "j87TNtn/i3TYQz+nLWB2+B3IoAB13bAMFreZnj7xCbeOhzvxphHoceeYZKawiAP6RwO3UHLBuOUHjcb7Sab/WQtk"
    "pd/3Ns0mnhVRcXwxToUTmhsriecSMBXizDk+kzHZTM7d+41tRy59Jj3iNqzmgKSQ0tZog5KI5UPkHVddzh78zETa"
    "7l+69yURej8YY6yJP1atWuVqmrH3TTrnRvdOTT2tSNtPLwqzBBaOkHJP3JDfsk37AGNssnbu/WrVlbDZuBEeHqLV"
    "q1djZpUp2yOANmzYIPB5NcZ169a59evXVxo+xznKz6n2uhJ4PdfrE4Dd7ooLCbyaAKVDPbZ+PGNsExFt6vsMORG6"
    "/t2NGzf23LNqXv3zWbt2LV+3bp1fv9WrV/vrb9y4kc0394OleSzM/nkd1JxBnELsowYYqF/p8cV7oCtVCPRyaFUK"
    "k1ei+gh+9Wr1wi6bpbq/3Vv3AelFdG95YihrG8uTrvHSsE/Twt6d+3ytLq91sP58KJMbV4brzHHj3EsOpYvmPc77"
    "rIltXkccSZS0mpwfc33c+zn+YMYFPlVOKpwbVL7nn9XV2AdzFUq/nptJbNgQPhO5s0I7sswh1TnwHWep0Kj9yg3u"
    "073kvx8uN896H/T4DnDf5j3XPPeyey/65ljfU/5y95IDFPnHEgPRBbPSGSecFs4qLrgpHy0EL3z7DqY1y6njK5LM"
    "UgsJ58aHPwKFPJAQC6niG4Dxlj6TechbKeUJfIVF5/waaK3vrE7CGCu2bt0anXLKU17NmHxtXhTPHojjY5Lh4Z71"
    "aqedQuR8eydNv8YE/b9EJZ8uUUgLCpGDYXAl0wcznPdhLJmYORimeKDrVXkbC7xnDvXYgxljXXgc4nx8fGX9+oUD"
    "ZiUs+KAswfo5DxZmWZ5/QUFVq2jA5rteqzVzyELEu18rxCFcY9aGGCBZUsqJPN/aE0Q/GCbuNpBAR+c5675+nu/V"
    "zlcylR866HuoDN4Ljg3E/VjCGOx+x43s/XUHfx3Ms2Sa5ffnu01uvu+77pzCa7+33dYXTkyPf6YgVhCXorS3cf8Y"
    "aWcpFcnMmp8tDviM9V0Pt71vPqVueID7Nh8dzL30Amudv675yUFhMc0Yt8zZyBkuiHv/rvcPBpSUE8HfS3t7jxtw"
    "TOyD8moIj1MIqAQHFmOCmJCOpGSSl36sVfNbHaV/2gkhvGuqKIq3xXG8sdSMizxvn+Oc+Msoii7oG3nXlWWNYY04"
    "AU7zRCLCzxqt9SeFEH/IGPsurIc1a9bMWfTdu3efxhg7DnkCldYrpSSZSE6avjE4OLirYk7j4+PnK6Ve5IiOi2Jl"
    "rLYfajabt7bb7RPyPD/FucJoPatBcO7N4keWLFlyb8lA3cTExHOFEJcJIVYKIYYsUWGN3m7J3rhvz77PMMY69WS+"
    "UvgZaPpve9vbLhVCvEgIcRJjLAbjz3W+Q2t9a3umfQ1jbE9VILB2vD/X1NTUslarhWtaYwwv+9XjM7yeHBkZubOa"
    "p3NuaN++fc/SWuMzJFBAMEnGpGg0oplGo3EHYwwuMbN3797jlFIY19mcy6WMETdG77bW3mWtvZYxdv/BWoJ1K3P3"
    "7t3PTZLkXM756Zzz5Ywx3NtCaz2R5/kPiOiu0dHRmxljew9Q56tPEFUtlMstPECHSAyxcv+o23K/h2QPXxEU7TPF"
    "QGdfKKWAy2y5XD2y9+7zZNFuFFnHpG1j4UzcpwQTTBSSkq8+83vf6IC53P7vzxs+dXTrqwYcncUYO9aRjAV3rQ5l"
    "3+3YsVuWv3zn9WwNMzUm4u79+LNOidzuFVke6zy1IlKFiCOlJBWiEEN6jxu95flrbgfkcUHCNLb+14ozecctVcIX"
    "21QyijgxwSjOKKfhHU9/1bfvLOueuqoSN3mGyOixq5c+P2LT5zZi9VTp6AhrecwZ6zDtdrZSvnVSiM2M7X0gxJ4O"
    "2D+o3L9hHzz66RVnJrTnhZEQT2OCljttm4JTzhztTdv0wBQbvvMTO579ZbbmhqzvePfI1cccx5x7ep43i73f3rxk"
    "gJkBIRRZZxlcj8D/WMdYQ0k6QvNzH9p4fKZzE+dWaScjo0ZG7zj1kq9N1T0GveNjNHntEafqfPqFDcVWxlwdmVs7"
    "aoUwkbP7TE7fmbTRHe+/54yb2JqbNQ7807XE+5MWq3X95oePO7mp6GhdOE7MRtxymXIlsamsE3d/8rvf3eHXfT3R"
    "rk8tfeGAa/2MVHJ50bRsT9b4pxMv3vP1H48FYslyI6ngCHznwbLHHMKvkiMK6t+FnSxCwkfIQvdILBMepNI5YB0j"
    "AW1NzplWaWyEp1gIASaFk0Bze0sURe9DDSMwqU6n8xYh1N9zLmSWasu5dLw0bJgPsARfAvNvBiaNH1g2QohXG2PO"
    "brfbP9dsNm+pM5nqdXNw5M+SWK7J0tQIRJXLNPk4jnlu8jcwxj6UpukzlFJ/kef5K5Ik6QqIzGYPEdGtSqnXJ0my"
    "tpO2TbMphV8La22cxLzVST/BGPtZ59yR2to/M0XxS3Ece/fcLEW45q8lRydbiqL4c8bYZ0qrx1tIReFeQmTewRid"
    "Xw5x9sgowq9fG2gOPpBp/deMsX+uKs2W6+GT36y1L16yZMlHjTHQyHxCHT5HTaiiKO6fmJj4aSKa8Pe10zmj0Wjc"
    "xIghEtbVI6I4pizPd0xOTp6BGFVRmN8lcr+rlDymfz5E9MtZlr2zKIr/yrLsLxhjOw9UzNE5FxmT/6Ix9PPOuRfG"
    "cRz3fxfzbTab/iZnef6gMWZjlmXvZ4z9YCEloXadkgOGJ/bxJvlZox0BF+IXpnKNBc+kscwONsa6c3w0a4+N8J2f"
    "HGpOj+SiIC0cmSSnMexXx6iVDz6XrWdbHvv8yC8P6nv+sGn0SsIWigwRxzpyGuQRDej8j7LPL7mR0/F/xV6y7Ytb"
    "V/pFzo8Y2XHBmJm4at8Ed3xYsyYXJBJBCuiXwSk6SrVeSES3zKcBV8z8oatPeNoyPv7lgeFsEAOPFZGQUCMlxSOG"
    "duzO/4aI3UkbnPcH4jzenbv88l/Myb5Bt2bObSZaemWfCW9KhQswGoocqcJOpNePXD1j+BWMjd/nrRGANedxx4V7"
    "wmly84rLGq7zRtvefWEcmSaSlf23o4qdM1LDjJJign7tlFvvfv0Xjvvgl7+18l8ZuyHbuiGszXA08ctDSv/l5ORe"
    "11RQaUVgVeWT4VUt4QAWpeFG9o6G3f0OkzBKM0tcONo5vuvlRHTNhg3EYUV6wemFH6P0puNextnkL+jpmYuH42LM"
    "cy5WUAJnCwdPZCSbnMbSnP7oeXfd/o4blr83funuj65f7+wcIbqBOK1hZsXS8Xc0uHlDp0VGcS0ER6FZTskgox2P"
    "8XeuX0/vGr/22DMGm/rPTbt1adLQgnRB0SCn1oyAUvXjESCSG+fVqlKbsn6Vg3KPh806ToJhc470dX3KYb/7GEjg"
    "MiEGAgqywfqoos5LpjFbjBdUD26CkQlr7V8LISA8IgiPNM1/N47V3+V54YQgI5UQ4bwYF3OcMxM8FEBzeX4L7amb"
    "KIcsa6XUMVEUfcI5dx5j7Ds1JubPpJSAtuTLXMDygODRuvLoiLZz7mStzWbO+TKlImQ2G1gEqKvEJVAGwdLAPCSP"
    "SMDyClDmMDBbdNptd5zW+nop5UoWroHj/bVqSX1MKXUWEX1a5/pPGWN/jnPnef5bnNv3ci5wbVsUhcc/9/vzlZSn"
    "AGlVFMVZ69ate/O6det6rDwICjDfoii89VGtO/7RGsnptRNKySXsR+WtFD82Y0zIvuMsw3eLQv+HUvLn/F1GV7Aa"
    "4boQdHEcw+f5ZiJ6aZ7nP8cYu6NfiFR/T09Pr9Raf0DK6LxKRmKt63OoD1EIweIoOomI3q6UekOapm9MkuQz/W48"
    "zLs8tsKN//DVDspSbbUlLM+J+2hMeygk1/hPjXIuzXNNmWNkHDYKV4wGHCcjDT3mYjN5w/DvD4uZK6gjKMvIaF6Q"
    "yiRxASU3KGecOxY19IW2yF40+bnR149cNPERf6sGomv1Y3ZqibDDmlmHzYzEBkvGKFYINelrfd0y3zQQt8DtG+a7"
    "zl8y5AZbWW5ii50GBohNmbNiSrWXJMlVfp77iLM3UbHv08ef1JS/9K9k3aoI5YoQU+j4wZYuPaDTOG4g7CCSpEaj"
    "KH09F3r1+DVL3sHYxJVwi9dXvxIeW93q6KRNN7y3yTu/Tga96CLSaWGcRoWL2e6lHC5EJomzmDeT4lkUT7x/1dO+"
    "8ivbPrvkZ49/+bjPaeJK5+Ry12DCQlXyodjSlphF5YHfGbAxqH2ehTSUdSirERYixCEqq+vRG08/cszuujIS6Wqi"
    "ghRnVBTK2hxqJyqJ45SmNCsEnheexPZsos7Z7WsGf2X3ZOM3Gdv1/bkC3dHgAAkFhcQwSDWnjcGp8J+Ydsnk9PUn"
    "r2yoh28SIlpKDDncTBeGmWQSEQgTeBH9OIgTJzV7Nys3lJ+W9276EIDk6m5RDwU6JazFypfChnnFIiBT/H/Ax5uC"
    "HMv6tUIwjSor2D8h1tp7OOfrSgaQdzqdFwvB3q21dlGkvIBBsykwF+ushQIH/uAZOW41dImS0VVMB6U7tNawLJYZ"
    "Y/7VOQeHRZVc16uZBqvF//gEPHJUpGlsjLlCSrEsz3VRClh/LfzUkh3RW4MEF7CN4fvzY8B1hOCJEPlVEB5FgXN4"
    "qwDH+nPgfJwLoZSqLAUrlPizoiheAi1cCHFlOS7Mgwsp/HxxHCvnXgpfq7XWUso3/fEf//HakknPckoG4HuAOJcL"
    "5OeKefLAZLvfNVkWeXSRty3DmpTr5EEXSZL8GYQHdANrfHyjWv9yPty/xnEoJSKlRBb8vznnllTn6bM8ntpoJNdK"
    "Kc8zxvpyzvgY50HmPDLJSwXBu9wwX6+mElVzXqqU+miaphdDeFTWW3lAZVVWf/f8flyPCxOceOnKKskvEfOyl0fR"
    "cPf9GV9zVDsqNHM5MqIMkyQZxYqRku1jmjO/Myw6V9i2sIXhlpT091M6Kzg5/yPw2ha8mLZwU1GjmPxg54axl+L8"
    "S5+/Yzs12ZfUsHRKJtZy48MzFl6etmU6y1/+4KbzEzCrMMJZWlXGLRpSvsj7CxxB+/J+HeRGxg3EL6MtyUUPf8+t"
    "JQnhMXX9KecNq32bImlWdaassRPYBRpSQ6AgN/cIfs7wiALLKU0kuHZOt7XmRg2ONTr/2Lmh+behlEzgd35c64i5"
    "LVepk669/t+aLv/1bGrSpJ0Zo8k4PCBKMi4k87X1wAPAmiW3Qsuc5Zmz+XSnaKjO2UuK7OM/+OjLxvx9KiDPQ9c6"
    "ayAk0P00IEQrZdk/+J4BMeJOOuEEi7hyDT7gpBsM+wiCcz3ZR25eedyI/cHnI9ZaXczsM1kbSVXcSWF4JJH2wARn"
    "TnBHguH5Nlow7Zhpa9vJOqaRpC85upl94dENx57h7wdckTVCwgNq4zCC5JLelrOccasFG+YyKszD/yisW5qPpwUz"
    "Be6T9D/gg6Xp8UNbID2ogHmIrZmNG3TJB7kDj/foROyCckr+v25VkXTu9fzNqCFc8Lo0TnkJdewq9LVhlLWIQlTZ"
    "wi1vfkcI0SktiNEsy8B0Y60hESj0KWQeGmyVEjzP84k0Ta93zm0NiqZ4dhzHL1VKJRA6Xtp4C0OJst7T84no1xlj"
    "/1AFdsv5Orh6LZg9DPCg/grMweTFWzSXz7TSM15VZm37w0qGputlVyzTTEk8PF6LQLABm+IyzkSUZxqMlsOKqJhh"
    "pR3XCIzXegi0M+/TBaVCSuaMl9IspGWG9cM1u0CgIJHRhQUCFIP8g06n8++I/cCfXQom7K0uuqmcS+X081y6viPK"
    "+1KBHCCrQv8WTk/hMnqzKTR0B+4Dko68pVBaMXXmjb9VlmU6juOnaa1/Wym1rrISceWdO3cOZFn2oTiOT8iyXHOm"
    "ZGgw1gU4eAFSnRM1vID8K80YADC5gZIg5SBx9uGZmZlnMsYereaN789LlSn7OAhKfuA8vsZWdUJ8gP/Z9Pd3d9dg"
    "wAATDwsfBUf9nSMmwBYZ6gM0R1T2OlsopwWS23LwXAOL31kD7uYzTkJ4DtufSVlYI5MkSo35K7d19U3s9I15pgb/"
    "synSS6nwqBdCbx4qOM8MtD575ljr/pVEdGeJtO/68r2zYcvYSPaYPsfoiCH+4Y1mXNPCgcWYYfE1yitUZNpfO/k4"
    "tW/HJ7mwS3WLdCRIFqRJGJQtspaR5lJhb4ZyRmlhLRMInnG4C6RhEu0dbDJi39a+YeyBgYv3/V8fjC7dYuPP+7Nf"
    "GlPmtXaPLSKllRWKHGekmXZKMCYaXnsKDKVTkNGQl5oLKzi3ES+ytBgYMmcTv/mdRPT70krBJFA+GjMyjBDMNV3h"
    "MVuoLOSD+AcXS2AEymeQsFnYdzvIPPLV5x4x8ti3r05458xOZgoh4A/RHtHlXZkEye3PBOBqyIhzJKwvNMt5ZB0V"
    "GdNqcPopzWZ+7e7Pn3Y2e8l3dnghWkLznAZe1TCrcXM0ccZJuYjbXNJY1HkDafv0ogMkMinEHbBDHPRQBB5FeEbk"
    "jySj/FAp+FzIVUIECYAsPAPBLQVtWujW1PGG6AddkCDLbOjrwTDsINlhmQZfRwjCC8ZpviA6CA93FEVgqjdEUXQz"
    "Hnp4R9I0/dU4jk/qdFKjZCzgtfeWh3UWTNdauymKot+M4/g79fO1Wi0EHzdIKU+CoAHfKxkbjoEm/Vvj4+MfAsx3"
    "tmjgrI+kRzt1RMNjo88NbqaupYRzCGhXCC4744vxgaq4ikeW+wCdv7mcokhEuoCGRkzFEkWTZudvwQhLVb8cBVi1"
    "XxcVPRU3psCxCEbIstfv7LH+HlXcwN987qPMiN80pJSvJaK/fOCBB3jfGLsPj2cUAUDTY4FoAPMKTZBvlZAKsQO4"
    "CiUzRjtjLUVxNMdiLgoISi+AutdBPCk8H/o3Wq3WBxljO6p7nXU6a6I4fkHpbpSQf+D5UBuUkojPZNba+5xzk5zz"
    "I6VUT4P9BbRCFbwWQoo8L3QcRcslF68jov9Tm7ff1DWkX/hdvh4cHDxEIeLXy6GAor86xgCBh+fFV15wdjpZ1n0G"
    "W2IfG4DwJRUULR6iw9wUxENOgvcAR9JxGkhCzBBMKYVbCAae5mQUeu746xWJE8wKO9BIz9SPXfcCIrqJJ8PXFzPs"
    "YS47x9mOT5H3EUHNtGkMceky/WIvQCpYaa0wZGcyfmYk9NGmgPkoOLIloLkbrkV70rWNZNfg6w996IR4ya6dH2ok"
    "tLTISCvWlCjhwnnqM1WjiPj0DNPKie8wIVuWmRMbA/FyQ5rg3UksHMWGkZYsn3TG6PQv9n3u+M/RRdse8i6lDWuj"
    "VvY3byZKbZYILuEaw8LYjFRMLDVx4TrqZinV901ujyPTPjdRejjLwobuxIWTlgtTNIxS7le3bl39DnroekVjQqDZ"
    "BOJKJtVwloe93POsWxJJEYK+GgLECWpAaw0BRrae2elzvvX3g5F7TjpNBRekGGwPKwlFaxgXTg5g5WIS8LrZ1D/k"
    "aQvwI4gUy6AwMCZk1iE9tIQfp1p73+fWrn0N4FpVT5ksSwuF2JpFPBk7O1Q70MJSosTpGW4ZR9FaMuCzBPPHaQeB"
    "JwyqH0z/cAIEe3nn7aeeNGTbp6FOlSUmhYN7JwfDsaoRTzbP+d5t2Mk95qwm7TWdgKXCIxmC4sG4JPBO67hN1HCP"
    "cGo0iNr7UMJBkGOhsGkQHoIs/IBw54Dh9Ed+AzMDg6/85f9Wvm+2bdu2xDn3G95n5n31eFwNFdpZ/F0UxQNpmq4Z"
    "Hh4G4qjnvIyxLdPT078OXziGByUdWjCEDqwSKeVTh4aGfoaIrq4Ypo94z9ZGqsckun68EGwOEtVbpsbD/VR1Dq01"
    "xgYtBNtkdmOCpzDnZBRiDmmafrkoii2YB+f8nDiOnweGGfrCz8Y1ONYPdhdycLgfP0uL7C6t9e2C8ZRLcZYS6jxf"
    "QsPCDAhCCMeXhgSYNVxg765ZSd18iEqIlIYQITW3vo6ucMYw44QIlTDr7p4QgOYuiiXPi/x7RV7cqLWbTJIYCKyX"
    "RJE6FkIP467NCYIArq8j8zwH0yuR9t4Me4WPgUmAI+B+gvDAeiqVpunt1trfbjab98CtCWtlbGzpS4RiH+BSLnfA"
    "70ufUAxh573bzjkkSV6xefPmAwTTw5LNzBwqjJeRd7L5oGkplEKwC35JKgrpli6d6gqlIREzIWLBeIQQO+4Mcg78"
    "o+Fjd8Y5nrR5bqJdOlU3ca7ud0aPMtY6P1H6mTb1UBSGfzFD6WAOCydkxItp8yoIkNHnPzKeXz92o1D2VzNHKCLB"
    "HSsIV/RjFN4d+n/KgEov5Z2zBNPcgUFwAMN8FwYbx4JPp/arQ3defj+Y3MCS1mXDsniRnmbGKrhaLFmJ8Q9aEVs+"
    "lYobpzn/02LssrtOXPWyYtsXfu/IMTPzSw2Z/WHk+JIih35fMBScdDY3Q7E7YmJm+m2M2FshMMe/+L6zBzvpc3We"
    "MIHHA/Y2+Ke0LCv4jmnDf375Kye+VN2DRz554qmDYs+/DMXtF1AmbBNzUHhiCoqUPOLUHZ87s10kN/DdbkQXjVYk"
    "+VDkJi7nxIeMMw6GYIDJwdfA2fh08tFcRd+M8yKByGZFHOfJ8FaiPTR569HPb2Tjv2xa0nClpPewcAuz3ptrijPW"
    "nhZfzHj82SbpB3PGj3I2viSJ2q9AeJCbyCEE52CkcCGLaWeSZvbKiee+/5VjjD7pNpR8X0ewWfx9BrvBnkI8OjKO"
    "CvAgrtDExlETwZWIuM7JpiQp4VBIHr8FUkX1p7+y4kwx8+hNjcIcYWA6McAK8cT7fEsiLWnmtuH3Djj327Sulr1h"
    "GBQFH8OozM9QksTHGwLTIMbTAhr0LLUt9l3wo5df98fhnepvP6kqoNIzZmfLnhQ/EELcUGVCp2l6dhzHpwU3FNhQ"
    "GD60ZahVrVb616XwSKDw1p/sErn1xTRNr4njGDDeEOwuPUx4ba29pBQgngBAK2Nl4SSlEKkYTNC6iWVZdpNz7hNJ"
    "knybc4X2ioCWfrt6DMtZ9cwRgkcXGswb43xro9G4qvoMPcnPPffctzDG/zYIueCQre4KPEdoMwHhUBTF/0pU/D4W"
    "JV1nYKeT/bpS4gMI0tW1/SpR0Vr7VLjIGWO7wvnqGdnY98G2wNKEGwhvfTkLKau8je4xlRBBjKkUyP/caXf+cHR0"
    "FEUaPc3MzBzNGH1EKfXiugVY3e/S9XZ6KUA07mFaZCdwfJX7GI0PvsMfmWXZjHPu5wYGBn5QxUyOPvporPvVkzMz"
    "w4PN5ocZuAw4gH+OcXs5XHhPz/P8mAsuuOCRcli9fubaXB5vHMSH3Lx+ELzBkAXAHwYXgKM9e2ZjINUIKngJ89YI"
    "vBq4V8KKAcunC/WV6cGjf27FeQ9uq/bQ1q2roxU/uGb9aGL+yHSsRao0NFxv6OAMeUS5y5/j0VBr1lhi+pOmo15v"
    "bAoAlWdA2Hi2Azlqf6r9xXOOGbiQtq+tYKSrfYoXo43RiynRPplDusQzLS0cExGCSepjrEyUHf/U8G9QYbyTAi44"
    "oibc7yYaMiJN9TV/d9n0K0IPFMT2fXx/BxH9dfumZbfKYuoL1nUS4413YsKiThF3Dd5aM3P9ce8avHjbzph3zlQj"
    "ittJZhDqDNnEZERTyKITfXj5y6e/5K6jmBpkNpOjYy948P4HNx91ucrEFiaA0sJOi7RyRduRKPJi4KjRV+2A9RTy"
    "yZxjM58afHWkOkNQzpRVcKuRwRYVjk1R/B8nXbrvhtmb1vbBD9zS1vXTvyeSiFBDK9w+gAxw962DO31XMfj2I185"
    "fgX14FTzD7auG7485vn7rPZuLe8ow1H+ySq0E1q/de1a96kQdS8jCd5rg1+65EPSV7ElKphkKVFDsY6ONjk5uLHj"
    "xHe5gOMuXqLj4fuIvvk4LZDNIaBIU2JVQ5ojTLuTWxLKcB2SG/y4jJVWylgWq3fdu/J/H7meZtym8nrB8AhB8P6S"
    "DxWuigWLo07AorZKd0+lqYZyJoGXIq6AF9ie5Tgr6ibMCCG+wBjbBysYq+6c++kqMYxDFfcavnEIMmtdPJZl2X/5"
    "4TCs5hzyDDaO4+uRB1JPUuxq95yfWTJZ0+/a6XXveP82xpkXRfH2d787ft/69fPnMVRlVzxLgfezhIpCAGDceZ7/"
    "XRzHV9Wy0CEzcMw/5Ll+qVL8IjjrQ/C5e1YjpRStVmvD4ODge8rrVJIOzPmf0jR9NeI+EJT4bnVkGbsY3bdv31FE"
    "5AVI3UU1i2TDfL2jwBHNunKg56BVPZh5HQWF+EopPL714IMPvuXUU09FS8rgvwxz2pmm6VuIaAvnfBDCpobuquIv"
    "R5XZ6nz16tV53sl+rsPaR3LLj1SxWuaMWxrH8XJjzNZSeGBf4GaEBcZjvW/fpkzw3Y2kAXAE3NAIs1djXVIUxdHI"
    "v6nPe046yALvHZh8cM8r814m+ugUnpxAsFTjvbNWjUdheYyj/8eHScG4ckFOCsdaLNqxUxz9S6ed98A2/zzuJkdo"
    "mnj6xnyt2/Qnv3P9yy4YG2A/TXlmObPccYkzMXKTZPnIkjOi/2gyomlH59zWKm7Z1lTueAhv7yxzjuW5c1GSjraK"
    "B55DRNvXrSS2rlQ4d1x3wjNIRi+AlxSmHOaE1YQxWGRsd65P/CzRt2jvF45+WjNKn6+LhFlhuLCaYKEyZ3iWyuyx"
    "bGjtuo2MrdtEUOrCs3A/sYfHSDZ/Zs+t059uXjcY0WuytCjjEIJlbsA2R9Llxo7jef+UK7IB8kkoYMteBQq+9AIu"
    "Tf1Un5R8iQ25HmuJuy2kaPq0BybsI+dZ2Ykzm+eJHs4LMd1pMZkf01S7ICzXAWm2kdz2G1eMKm4i5TgB1lOaq97J"
    "AIkbpWZJGY8Jru2nEKfnkN577ZKnDVDnkmym4WJnOKwI6z0szkSDUkzMqI8d+cq9V2BMm1cRX4X7B5fUSufYJZMf"
    "bG067tTm0OTvFRMahwR4jjG8aOEesnPedtbJJ7OX0wPlc4vqjsBZhNYYJbcIi9Z0eVLoKRr942UvefRvepoo1eiH"
    "cmHJAuthnGaOR4gMQ5H0ugXwz5ycmSZrZGv57mU9x8G8CsvW9XjPfWwctN7e9zoSPI+HjM7KvTFb4yHA5ay/I/Mx"
    "3uoid+Gfe++9t3r/GbOfVUzf+wvgQ1RDQ0Pv6nQ6BVwynHOvXkHZLWGxHo1UFMVPwbqBBozjq5LlYR5uBTwLVbmT"
    "iof4pxwwPHDOUmOGJlwUxbujKLoSzH/dui6z9JO88847+VlnnYWGRV0mBZcUrLiAXLI8y/RMHMcfqIRHBTEtGa9p"
    "t9NrlRIXATqMMVdJlRh7aUG9py9D2x+LxMeJiYnPR1H00qo51Oy98m5BiPuR+XdKPfweeoD1JdwB29WjrZfrCOsD"
    "UOCPVMKjrwQKxnh/mqb3xnF8NsAREGx95/Gpeyh7UgbJEcfqiWX1bBLmfaM9tMFt2P6yzqW4f34jA+ThLecwb8CY"
    "hvomu9C591tTbSEqNLlEeJHgYx89dWU48ei4zuyiDRKxiVmtCkIHd1Mg82rAylaHNpx20QPfB0NkZ81a1BAmjF2g"
    "f/9z8r9Ixj+ti9gJnpOo8q1MTMSzZEBt9pYbe+kXxqc+PfRxKYu36RQw1OC+4oiYSCdYu/1KIvqMtzw2+6fdjqmZ"
    "FyWDNJjtbpqIpUIDpGKYFZwJq6MvHHnpfY/i3MNqcpVqiDibIsOBCCs4ZZTZRFqxb6Z5/YmvGf96Oex+t6FHDljb"
    "+kiho9cIGzMjc8/7YldYijnnHQXB9inBG49RlnlcqpWcBGJMzgmTkotcvmZiY2Ogw5Kr2rn4GvuFXY+FyuG+MGt1"
    "bYK7qZ+AZmdryG27YdS5ZJIhdmALby8GBA10P6uIu0R7ZNSGENAvc4Xc9E362Yl1SWdGG8csMqq9NuM4E7lmWYcn"
    "f16WoGEXXFAKz9IrBKGyfenMe9RO/RvM8AHrDAQk5DqzTJmhIRHvK8YB7vECxPfiszoIEA/lCt4C7piVg1yMa/7u"
    "ZRftusJtYII2EfPKRkVlFv4PJUCU71MArBsj54NuYCpAegG+ZkiitDpJF516WrX4njRgvLDKcjyEUKEDSImVcwLv"
    "g2XRb4GklMCo9sEe5mVfDk/wrCvLa61IAIrn+KMr5k5E9+GflStXhmAn50fitwEgRQVtHqhV3FAh+BIhYmi4ByQw"
    "E8Qa6jGN8gcMDJqSFyBAhZbjCUzOQgvzW0IUupjQWn+40vz7S35s2rSpZBRVDMK7u8oxe+AJS9N0GxHtLOGqdcns"
    "IbXtduYthNKFV1k//nTW2inn3LYa9Lbn2IcffnhbkmAqHgbdFSIeLRVQaF2pUqKzqtUvGXq9rfAsIS+k29yl1zrj"
    "RZFRnnfuKecyn2uSpXm+sxrHPJ/3xK1KwVoJV89At7vtTTWlVgiKlyZxfGQs5XIkBCA302qrdEcv54wdVSG/amvm"
    "x5mmaR21VcWx5tkl9DiC6KDI+TbOJfnrl0gDoEVz1+iek493oGQBJEcMMFJviwBfiqAYJ6FHbneuDQbUq2TtDvrY"
    "JC3fSXyqLPmLHCKUOsA5JDGbu2GXVioQcTX4H2ne/k3FqAEzCYxAcMHhhhCcXfzop08/krGtj7myEhqz+dlUFE55"
    "byJ4BCfpLAM63TpzdVmLwhU5P0sCbeXwnCMwgfAcMVsAIDMz2P7swK8g+d14NCUiRB5+yIR2ijjWqnMGEIlCQtIH"
    "1BCzuW+BzbQ5DmPJ3YrbxNSD44LZ0ZAehUx9MHePK6GRUf2yIZO/rGPs7tZ1o1u4ZVsKbu9K06PvXXbPfd9FoJuw"
    "CleRAmqqv2TK4NKGoT2FhgrGLbJQwMiD4WwBd8xbvYrK5vBQmHzwqcT3uci1nCPly9Zg+aPIsWndvP87d3zsgRWX"
    "XuBDd/XDPcLNEfundePb3/7c0bubsn1ulnmJJTzIxgnkmsBtCIRc8P8gwMUUMYlJo7pHML0jYUWR6n3TfNlVzm3j"
    "KNu3pias6vRDCRCrUuXD1YClGlnWIg0OCm9E+xQFYo/ubvc8TRLON2+W4zmuj6urbPtyJ70+PqLEwuA0ZOHit7FP"
    "nOmCGn1CkY8DkxNzzK26Slqd1HfOS7NsMKC5iCHhF+UGylhv6IbkvBule74qGa+iMl7vmVKdIdeYTJWdXZ3BI6Nm"
    "3W/QMKyTQqJOyPcajcYj8zB/qgo9ltfsCqEKmVsFxoOXb64WPUuz+24ed1vebDa92T4fs4axhTnBcgm4m1kQQNUw"
    "qfou3OE9N6B0s4UKHJh/sKLK688GY6pReneUj8sURGLvPEKtOtYBUeUHPH8S4Jw9XgnmLMtON9a+waXuAqfcybAU"
    "m7VkdL/MCpGAuNsMKyQV95bFkVJ25zoXvzFLFZSYDpmA2Anuv24gvcwpmAUclzQwQAxo0DKv1MdrvPcc8XRGuZXT"
    "/rZ1YQW1dSFye1lkSYnwePozIM8Dz7LPzIYCXSuxsf3umWtG702i7CwNiKtHtxDTKblGwo/SZvy5yKxGczh32+rG"
    "zN6rn6dS4+HQAdEnrYoET222czw5dtOx9G23adNNMp259IwmEu3gheHwn3KYRxzP55CiCynOLySRhXgr9BT/nHGi"
    "SBL5ijeCdBrEnn/KDKPCGRanBTEdNTGzkRd/+/72Ncs+rgY7l7OJmUJwqQxiMhKxXEUug/pu+IBiy0gWFxPZi5Pc"
    "knQ/mE5/Krp7+rOjG/Z1jvl/bM23xvvzK0BjA7Ht7PRQTA/oxYKGewa+gC6TQDHV+Fup3SuenUQcKWDepRbuC5ia"
    "hLXmHrlg/Yv0gq2LNyLexM3vXjv2IEXsXELz1qpWBywfYHGKfEVXAUikCkhjIPSwv7ExEPDnLNXmeye+4mGvmO2P"
    "fqhEQt+Yw4XsGY9PrtUGrZ4oMIGjvI4/S4Yh4zj05wixwF6mHJ4R4Zwd6F2kJhpKxYyE9PAyMsEP3WXqPm8Az9o8"
    "T/H8/ue+PILgaw6TwIIG4YZk8HpOQ62zHIKo0Nyrwoc+MQ/bpowbeORXn8pduYXqyWY+a9kYs7sSHgcoBjjnvtU6"
    "4B0AVh2Wps78K236QEHeWkZ5V4BUfyNjvQxcV9QDV+7+BFneg0YqBXTQwKBSd+9VKH9SZ9DzLkZfN2Kco5ZDMzug"
    "2UTCo/I8h6V3RyNJfreZNJ410GgONeIE8S+fLFiWYClqP31lMHoSBesWyLwxkOrv6enpQ46kK3BAj98LpSbCCcu5"
    "C8erWlj+7anMWV1gEgTUWCARYL9hhfd/sTJzw4M6YUx5HzmYYHChVXTnB8HTmYsa0Q00oACwJSCxyOZQ4gwTuUvc"
    "Xl9LzrtmJr9wluJ0UqE5GBTwV1AkHBtgcGV8+tgXf2cvGOOy3Zs5s7pBGvcQbqXZahMAvmnNnGkZXUwbU0xbo9vM"
    "mFwakwtTdJjJ284UHZ/BFzByiC3ijsNJBE8cOgOWcY00PvUvdRbfrwYaCskbTORGOOkUy0lwizIUTGfaFTPa6Cmt"
    "TdvaONZDyaA8b7DprjzyyJ33TH5x5A2wPqokxYr2tZrI0kRKelnDBm5PiQw875aVEfC7NYKrz4vwyWVg6jAcLCu8"
    "YhiSRgU5XUz4x3ueasiz5CizfMb7NpHx4J0cjnLKPShP8twn13qKUc4CjKoLY8KfjniDUpbs8QrEPMKxTpWfHuqd"
    "WOAH/u95T6J1E7nBvoJIiH3ghns3DbDyyAvFnue79pYJMl3yBU2Dw6oWJ64zDmRo8mK697ppBxA1JN6ELE9vvVSM"
    "qdKYvKo1r6ba/1b5NBUVE6oWvHTSe4YZMtKDnVXPRK8yskFVZnr5OZdSoB+2f+2cgyOuXkqjZgL0Dsg6V0GT5mUy"
    "Va8S35Cutl5gllUCW/388wmhOoOrC5Hyvf366Ktja3PpCu+yJEuP26s2Dv971uVFbHBwti+GJg1nhM/erc5XusVg"
    "Zfq8woXGVK5H3ZrpF4SVieuFR5qmQNx9Tin1OimRSm290K+AFlgS3ELENkrotP/xXqFabkf9OlmW1cfX48Lq6wX/"
    "+GBY2FosQvi1WwOrMuesZa4YSWahEA3ltNHOWV26PyDog+CB3iajWWt6PnLg2pCdBrilEsFTGU61TPjnnBreneZL"
    "r8kLYaREzwuo2f6OIKEdtQjO37RprbcABelXxA08N8LCpeJHZguedbQuaOzDnm8xcgPLHuKSyZi48rUV/WUR1fX4"
    "AZxeM8uQDYEbIjnyw/1vr0JpZNALjso9MCAAavelAmD1hOwZh67yoJXElrz41m3aHfmy3CVfkkubUgw00DaY5VzC"
    "2DEOSbhw1cINypk0MbxRiaMON7qTmsjMHDtM6b9Mbxz9Le9CqjFb/mjK4UJjngeCH4Y6ZrNbZD4ngc9j81aBDwXA"
    "WeHXvwsxPajKvbZwqU9uKvcI7j1HnTPeJCH9vi6/hxIwQF/5YEjJO/GJplyzyle5X5KHUk67nyF5LgbXlc7DnvZJ"
    "TsFj44sNeogkRSr9fo9FIGxeeOdt6P9RgTdnz+q9qR3BBu8ob0hInWwMbNNtvQ+mZQgMwgz2Nnz4Wr0B3ELzQG2m"
    "8qUPMLezdDqIn2CUg8khcQ3Cwxj9g6LQfxuy0RW00p41Kv8sK/oaTNln9nKO/L/goonjuPXII49MdOdecz95huqN"
    "npKB99V4mkur5riH6vWturPfL1U++tl3KghtWUJkwePr7pl+y8XX5up2ggxDW+g8fqWHZuPOWDyNFvci1LfpsVi8"
    "7KjVvZmHwrr3zKfLwEvh4F9u27YNwvz/SSnPRMkTVPwtlSTvMoPQxxeLooAwhNsMdbh8aRUpJWDUUV1oV+tQVnXu"
    "Tqd8fz6h5oaGhg7RhYWNDke6Z7Fl8m3I0YBfSmun46HZmB9QWFA5fXzMX7e0OH14kkNd2u9aOo285CKIXeQR+H1a"
    "MiFybJ8Kx6/bHHzumzevufNsd8VXosS8wMwIj6BhrGA2ww2Vpz/Hvfepmzad/12Xfu3lVCBYC7Mc/nZrVWxFnkd3"
    "fa7191ucW+P1mdbuE+2ypsqhecKiwZb0YVIvmyyJiBGPVFCdS0x4SKi0Pvner3UoCeUjpXBWej4JpswKzCxUSby3"
    "ZD/sW9+96qrLL/yFZ2/82TgTb2QiPT9qoGRHyeMLAUbuk/WBDOKYG/LdHVJajIFkiZvpe8ZvHL2FXTjxDbcywIMm"
    "RyZpeTsk8IQYJdyI3geI0BRlRvU+H+uCVgDAFKK9ugRQdlkueCKfZf772y/ocdzlr9BtfeKA8vefcV36zRgJb+6j"
    "WzL8hb7enx+jtCnZvG98C5AfkHP3x2lx3Os4409D+zNfzjC4RJyUot1q5SjdfXe/EOHMhGzKrle4xmCsL1Dlfd1s"
    "ujdxDP1A/JgNI+tj8GXAofQRYrMYPKv2GY7oK7PtaloDjsweMsCFe6sDLVKD9ePNRKGIKUZQH/twvD7a5peNMQ/r"
    "rKrGOm0e9rU0ZYmIKhVHfFUInkrZuJJ+BDS7dn3aNFvYFdNPq8rM+iA/4L7zQdTyuPJ+18pwLOAKK/9mjydPIRSn"
    "CvlM1RIu9N3aOBA7CLck7BRNQzUNrHRRdUMcXcsoFLFEEsJ+B1ZWQp69cD1JcjbQb9vt9qviOH52nucmiiJVE4Re"
    "YUjTbIvWxb87V9xjLX94ZGQE4Hz0d29yzm9EYigEUr0MS79lUY+Xzc5/vuZSB09I6wsVlKElhsJ5QecBAINslg33"
    "CtCyiFjoCBOeEI+XEQC2QHtfmEwqrVUIZSPYUVoAPg4BRqS9MVOhjeBKuWDNep3fOPqfjugF2moWcUOaccQTbSO2"
    "8XhLn3OK+gGalpyCXnAwTFA605ImliSoIXjjmlesMUCBwWHG1vx5Pv2JaBs17EqU6/b5CaHoj1UDSBCNP92JB94r"
    "OzMiiZ2hVFLqSx4ZS0JaX6PXadTEgpljwU07qmmL4mjHsh2SN5s7iXaCYYc18pDsDxZvIvoYEf3nxA1LnqvS4lxn"
    "+U8roX+KOD8+avKEA8TQ8q4gJ3XMnMxJWCGYM1qNRTLKxRuJ6HcAifYLCciMCk3yIC7L4g0hZ81HQftuQ1kOimm1"
    "A/EdPAzKA6gRLo58zNc2zJg/ePU8/tkaNRT3brCqcLuH6MIrIwSlOiqVWQwIoYTCt89A4mjw6gReAuh0rar8/gWI"
    "MSdenij1jwt9KY7pD5zLL2OMofwHp+BJIWMYrh6whDX0fdi8ZeaYc9o04t4JI8Mf8qn7IJTtaUNqm9c0jBXGFiM9"
    "3KljByVTCRc8JL2EGxOqHFYNqWD1hAyZORRYHufPIqJPVmaLEAK1rXwwusZ0vITjnJ/cbrdf0Gg0vopSFdu3bzer"
    "Ki4+S2zz5s3u1FNPjVasWAGGs18C810oztqPGFr4HAFG3C0oWRdC87Wom/9aAUZcpYcHZlj5SOYlWD5+/4eE8f44"
    "Ts9356m7VV44WDzT03PeneN+qvbEgQjKQYBRz/0MnrDan68pEWVdpu6LSYYS8zckSfwqxpI5+T6PPfbY4OjoaO2c"
    "3Qz8bmB9vnFVYIqaG6vytB4a+RomZdsDqpSH6h7OBVv4Ymue01eAFNgOvtgSGVHusTltAAOBbfTgN0IJGo9MIpTW"
    "zGv7o+xiqJKTru6MP/AXMeejDuAHjvJJwB07avCpVXomGUwUZxnMdICdrHBSJiIv8qylhz+Okhi0imwoM460O3k7"
    "6dbFBZNOOeWrzROLHKIXWcaj4fN23Tj/6OvsAjmgFSGwXjkBQvPQKgjdF4x2oy8d/xoR4Yfc1tURPXjbCVnKTie7"
    "71LmzOtQpb1ASiPSJqwkFFfkbcxVPw89dGjVej+IEQDaU/g4tA+8VCASLA4KLsRR33PmBY8jrUa/RzZzCcqZKWTX"
    "wMuCGkOIqPBTt214TYNoYzonkI5btobsli3PVtGj3z4TCgOD/IIJBhg2ZA5iSlnzodl9Esqfexe4LwAYEgkBikIx"
    "SToICt34rH0uypcXRaGVUKJ0sfvNiaQ1pdSwtunlFLC43d0F73HVDTDk9wXrIVjXqLKmIQjyIukVBDJycIYSankF"
    "+GYfo/BWqXVzutIIxZ1CucgA5AXiK1QnLN2z0Jq0Jc3L6Ld3+dzcLQZYnuXcklFXJTe+VrpuuLeKvLnOEBxHTgSK"
    "/v0mY+zLOOaUU07pwQfce++98vTTT8+zLHutEOJteZ5/g3OOTHfgrHfmeT5ZFMVjg4ODj9bEeTm2Ht94uYYH3U+9"
    "64YKJSpqH9T81Ps7Nny399gy1jBHiFXtd0Mosuv/99xptqbWHB9/bQOGhkrBlVs1V5rNRMcskJcIK6LrcQrp6v46"
    "SqFA4P4o5KsAQQttC7d6VrCF+ezbt2+Uc35aBYKoM/Uyix29TVKgGDdu3GjKdrl+Pvv27Vtire3muATXQCiPD0JZ"
    "mdpnlQurvq7d+3zopUyQ6yWZAlqGQk5DKAMUBgfdPI6nuudsdQo24IMHJUjFo51CtYcQ9eutcTaXQnPGAOKCjwf+"
    "c7ijEZNwrLO7BjoJwWM4J3Z0rok3cV68Ums0ayWBYrN4Rgcl+xmyU6fDnc9VBGA2WZdbrqzIXHbT8sse/Xq30GJZ"
    "6FDI5t1+sNJwuNPgYTCOeDHTIK7b5z74sSNPPFE8tqM2YOoKxTuJb/z+avtsvukp4oiVu0+6YPPUvCVViNOGDUYs"
    "a61Tzxy4Jh5rbmvQSN7clzb12Pd37qQxskiuLHMmHiDiVz/26SOuHbWtj3CeDgATq5VhKLFmtGHWZctf+ax/GGYs"
    "SCrbiCzNoAAo4AUejOoLhPqChUpSkkaJ2zAjUHl37VpyNOSfJdbazLaS5iyXkWBRsNINEmFy55KYjm+OfPE8xujz"
    "PrGxVhljy/8lherFrYnxn4oT85Q8RXp/yVj9XrfcpIwKrbeUd4/yvMgT1G83oWReVevNu7wORYB4uAP0E/gKmQdE"
    "l5u+KkHhA4s+AaAHyB+yOcJwPMoDu6SsbxVKbCL53h617KQ+TiVU8MeVyA4PZfWQ3wD99egSKwZkiVSotCVvqRaw"
    "T8jZCFatLxbXLVSG+F9ekEuifswyIqC+BDoRnZPn+elxHPvcgj179nx9aGjwm3GcnJFlvsYUL8FESOpzcRz/gnPu"
    "K4yx9/ad00fCptL0GZzzvxJCnCiEeF71YZ7nYCxIbLuCiP53yZhrfdbndW3s9+HevDm45PoRRmFeVX+s/fPaUJi2"
    "J3Be3r8D8zUhhBcgQbMO3rIaGst3VpyPZmHNyML1AqQ3E1350JBX0CshAt0Eho6vC7zApDaWxfqq5agQK93S2WFf"
    "VhMbYyzk/FS1vKq6XtbaXEq5vaqavHr16qo0vS/132q1XhrH8bIsy2Dp9Luv5kx3PuHx+LLQyxMyeHfysrZVSA6s"
    "nkIZcdFszrrUgE1AzYRgnwc101tMldQ5CC96JWxQRd5Htr0fv9Qr+jsqekSQM5oGPmlo6lXWGcZZhI5xDO7VhDeO"
    "JWWOLeBmlJEvNWOs71RAeTG40VsfZaHFgERCGpe9I3X5ZGSjEYC/rI6YoxbgwWYgYcOadX6BvYLeVbW5rY0FSXnF"
    "D67+0muXypl/oX1bdk9dPfhoQqPfKnT7nowf9b3xlrzjqb/8vd17PnvsRwf4kWdQ7JRmYiilNFJZp8lb5mvsTewC"
    "d5VT/pYF3UnQU6xjZ+3+ZPvqgf8dNeKzdG6sQzcFFMTyt0cp027KysIxaWyNDnBxv/7+gfEqv+WRQSX3E9irZ1sK"
    "V10DpzYlt88wtSMeUkczjbatKSq2eREqTSEaVl+xacP5t7Gzbp7x7uQ/7XaLLDxDzsfX8yaTSNsPWfie3zopDZsp"
    "osmscertISzgJVNZi7/Eg3e1wdln5EDUs50qPx2bR9mdLyQqBFIqBaH9hmdqXmghF6R+BiZ37ZuFGYJibK0Sulsq"
    "OsH3UlZg9QMTjuWiRGGV5YcbuTFtXViLoCJQ5CUuvsaRS6RiaExUH0RoSlSgGi+KHv48Ed0D3rVs2bLpLMv+JvSP"
    "QKwkZHYHlxaClD6gfqXW+me01h8xxtyBXIlWq3VClEQXO0dvkVIeVRSFb9pUiyf46D7nvKp34yc2e18CRt9AyJZu"
    "reqmoTnT/mjWmMJxpbP7IBlUPf/iUJka/Puzri/v1+iJpfQJkO6OKds51lFBbmio3iRRdoffFY6hernPkQkYnLm0"
    "enVY07KwYVeY1eeGtiX4XRQqLf0aSysLoqxlBaUhLoritVEU+aZa1fKAx2VZdoaU8p1lN8WeJML5BEj1ncrNVY8v"
    "4RyPJ5HQw2lR9LaMC4WcEGwYRxHS/fbVMtFDeRNf7KGsIVoimVBxF9Vmi7z+TPWTBPAF3QG97hYwFd5es5YhqUGr"
    "Pk5QMv1Becp1LfPth5qD7RNt7nu4cmEY5aZwvpIsRgrXMwozKsdbRTSR5qfdSLSl6wrzchaNlC7es3Pm84PXJNz+"
    "omkV8GkJrtHMyfB2xl0Sdd6pPzsw9dDgWf980gU311yOjCavPuYSxfdcGdt8gEsxQE11IkX6bJWNUsz2kWZiFTG+"
    "a+iG4ROjATodcQ0UZkXuTCd1FFP6gplPHnExe/We6+lN3RN7FjNzzbJnRbx9ctsaF1nGlFbwdhATEQnlJnbtGeg6"
    "ZpeOrsg6419ve9EdbPUQV3KOmxajEUaX7726edeudNl3uBgthqLWiBvhu4YveGDP5E3H/4tSu/9EZ+hqhcLtyG8p"
    "eF4w24z5mec277i6dc2KP762/fdbGDpirmf08GdPOmWZ2fWnys28NBsXLoKrEuE4X3JHWTEiBJ/mnzn6ott3IfkR"
    "1orP2+nu4dm2mWF/Hdze9E88PDXlDQwFDUuNsf6AAH4352A5LbyjjQqfB1Jhg/AafliPd+Yujnbf38NZtNVW+jY+"
    "qDsCtIXvOFma5Mia9MgJ6wxaeNd2e4zUSg4wPzSckLteAocCDlMQXCGsBlXzN36GnFIItPrdD2mNFqZ/DcdoqXVu"
    "yLLs8iTxZb6NlFGpbkGr5lVBv1dxzl+ljd6l0bQoUkuUUD5XPi8KjKmLroIrMIoilDn5hG/vGWDQYS5lEDK4dAB+"
    "6KlsXgkQt379+jk3q4q/VG6SXnRribaZ11zvoS6Dq/vna1bJfhlclfNXYix6PpvtrNiXSNitXbbgaSsjsnsNmPph"
    "fF09Y0GqKhdDroaE+Nmvu4D1ps2br9t16aWXfZ+Iji+70PjKDbV415/meY4ujR+HIoiso6IoLuSc/wnn/Ci4uWCd"
    "VsKjD4VV70hYQ1/NLmeVlnBglNxc4sjnKG91ZemjJKfXaJ2z2/PZTHQmCrSq5Ch+ACgvMrAhPAxQgR3EQtR+kkxh"
    "0WjBG9jDEQk817DqeeGBYEjyOHKoNyO53EKcsdvHi01LrmNx9GY7Duw/mGXIDgnaAXLFOALsNmkqroy7/dhL7nx4"
    "TkuIMpisWfyeojXxWsCRNKotwr/KNMQ4MaubIjZXHjtz5+s6N7JN0+0jdkrZbsq8OI/bXRdJMpRqxBoc8U7meDqo"
    "qZmJGUMPLJMn3+HrHUbsy8Syc/LMFNxo6UNJKM0h0cdm6t9nrm7+FRuQm9kA390oeNOk7Kesa60Xwo4JJInDOHAF"
    "5Y65JgDEVmy95He+m3k3nC/z8fE8v254O7H06doxvx6e3znHAQRNZHYSV3SDoolJayaLpJkN75ka+GUUms/E0g/k"
    "7d1vFpQvKbSwghT3Wfmc8aLDnBqgC1k+8aJXNX/1q9ln5WPMsoEs3/bcODYjrRZzMYrEWE7GwxMNaqBRnvFUq6VX"
    "YGvfOxbiLQBghW5ZeKYRK5G+bhoEvUZG9UFs1W5Ntt5ciC4EsuYumS/yH+uAdasuFL7v8wvLxD7GmZhGq7EaGRSf"
    "6eZvlO0ivUYeLBnjDMp7aFTz77lcVAiODmo8Aiy7zNeokm6RROi7y4SaB56Cy2dw0NdZRE0EKjJthZAoAPd7VQMh"
    "wDWJ6PI8z7aXjL9eb6kbEwGjUVItl0KsiFXUKHMInKol2JX1pNCZcA/n/B21dqqlStybH9HnTqquewDYqq/JNUse"
    "3VHdpdn7NF9MJYrQV75bfqTHvVLmkCwogPwzXB5XoZ5qyYg9CX9V98S6cELP5a5F20u1WpKzbp9Sgz9g4LmyfHrh"
    "v7OGFv5B73LOCUUvWVFYtHb2je1C8yrfgVAqpdZrre+y1m41xtytlHqfEALWpUde1fM/as/IvAmUYc5hqjW5vN/1"
    "XWDVwcR12UylzCwGWhZIFEPIczPm+J7nBGhPxmUXwhlcUsGC0QcCWeAm+b6EobkSsECsiMC0SVvuxvXYXK6yMUBp"
    "SMqr9Yx2WjMO4YXxBekPN7PXdwEZheONpbr5KT+hvqS4qnPe6IvH7yiykT9ANUIrjGHovaokceX7org8k1Ym+qxE"
    "yj9Y1sz+bizO/mIoMRdxdIpwsFIEU6HbM7M0w4U2wuglf8Yuu9MDXZwd/rBuZxNOOMgPP0Lls905KW6OGBg2V0Sa"
    "fTWaKr6pO1N3CTnxUUn5U4uOdREsXiiKDJazI5tnrMPjD4Wl6Lr1qJDxXZQoxrlEH54yiTYoRh2ETwy5gdiNDDVp"
    "adxAV/og25eff+fOQkZvQ5cSDyJHaA4BDYtERMOKTmGZy5mMzdnRAH+FatKFTeVGUi1MFEGVReAdGYLcsUwaMZAL"
    "o+M/XvLi739zwwYSK0uLKhEJo0ghN8T/VGEz5PZZtIY5CPI3z5jCb8B6Ehce3sBgwv2FEt5/sKVBE56dipmU1WiD"
    "D9o/Qc6ZrMlHeza4KNtz+wgcD1VG/Rslyr3K2mwiWl4PgiAtyDexxEMx+yh2mY3345GL1WyP6pL8fQ4hXGicvtT6"
    "2/M8/2l4thA4TZLkPsb4q7XWO8BMkIkcZEZYkzJ5sFJvvcHj27yWnESEWCwAB1iMjpQSwPb7+3ty94amZgV1Nc6D"
    "uWmzDLV0g6EpUqn9HwgKXF+Qed47gKY/P0isFCBVY/t5xlGaEDD3vQLN2XRNKEgJHEeo6dpPPts5ig6K6dbjOjVh"
    "1G2+Fcfxv6dZ9lgcQ0mAj5j3XKd0Z0Wc8xHOeSMkpBvcUzTdQk5I9d36cf2uu57vePCLX5pegXYo5AuRlIpZeIkY"
    "EdA92idszxx/dM85ncGmKDVev+eEL9EjkQqAjIYDLmSACIda6gEGDIHlhKWpmVkXaJegcQMU2jjuNptH98c8Zd4X"
    "XO4D7Bvu3SnOqtjx6VQ/+Gh24ifqLrCetfPBeccH1uz7h7YY/ujg8lgmLDJCgguhhBF6g3V41i5sMamMbqWmmAEI"
    "SBgllYuQW4j4LeA2cDSNcDnVcv90xMXbPuaLDm4gkVy47VtFOvIun9woCyS8ANoIpBNyRlzekbDb0FhhiBkTZ9Pc"
    "opUtWod67COXlrOkGBiUYjqX1x150WM3gp2tWUOm8g7KwSX/ZSx3El3/QnuHkj+i6GTQoHWRO9/vJ40ctQeDzbqF"
    "1OCLJ/9tmpa9Mx4ekJit5dzANkKMGHVdoUvkbWfzGfLZ+BCaETpQI7aMXkHoWUCpiQatHB8f+NfGRXv+DoJ5degQ"
    "Gyjym5e4FF1QiK8clBsyBdI5fH7Kfql0XXnGiCxcuJzDs1wyBLiJA2PwPbV7D+Y2QW4VRxsrr9yE32WKOGxYVCfj"
    "ZrDTs0kMaQljgaFHI4peIbsmVNnRnHHNuNRCuCQL/Xe78qOTekEmjUm1NZlGH3ik3IanxaITMtJnmWZJucmDy2dm"
    "ZgZIdm/7ckCekWJqDVxsH3LOHY1AKWDXURR9zRhzYZ7nm8oqqyXUlxkYIZ4ZhaRN3+YUySUOJS9LCRM63JlvEdHL"
    "GGOb+oVH+Wz6QDqsCAF1guG3dxDMNldfmErXlXeX+eIMmAgsh9IqCVlm+7vhs9+rrtn9fSAUWBzHSL4r5jsWYYY+"
    "Tdx/5teNMyMk82NEUQyPdkDwtJqUU+Xx3qLzP9W5Dyb7ttyrplZOZr7jYSY+yojeaK1O4wQN3/26Vb1IKksTpUy8"
    "MgDdD/vAWvuvjDE0nYBAwTz9tTCf8njRJ2TLcaATng9zhXvU1zf+YAkpfKh2xzE/x9AdzkAtZ0yapuBs2e7WbBFL"
    "yYwt8swWKbgA4EH+e0IozSKAOqpu1vOTQD/WvDAoU+u7M+A/hn6NkYmYZUc0JucIoLK2lmBnfb1dcH6t76dKUjMh"
    "wVN8c3BO2kQa3Ze4zZnd8LTL7tzjj5mvplMgt3at481L/vr1LaneQ8uYlLFCQXospWEa9QnBhHPuywWKGGnfUPOw"
    "wQCetVIxLgeEmmw33/P10d9/s1vrAbEOcFc8sY2Xj//NjONXqKVMqhHBSSrLJYQQMi8yznTuPGZHCxejw2co4W8g"
    "HL2OP6JVahuf0cvP+Pn6bYUQAbOOn/+dr+ecv1M0LEYF96cueapDvVYUXYLF5AyarBe+tDeO3/xZr8vx4Zc9+q5J"
    "wd9iBqJUDSMbHoIBPAMgN9/2C2oPBhVw3eDeDi3DhBNwEy4ZkFMk/+qIV0y9Ef2Y2PoSsFqSRVDGFxf07WV87RiG"
    "1qPogOUbPB10JrqvSyyiOGTi9mxenwPvIbQV9rpb1tfK7G6ynMlRl6AAm9dDvNgJmGOUlrZZY+vylb/Vce7mrs1g"
    "m8mDhTOFGjWR9yUgVbTOOyNAzKJvjC1bugtSfd26jd4wonuO3dPOdj8kRvhpYqasR9nN1EQJUENp2hzP4iX3E233"
    "2PIyI3iqKPLHpOSzdWACPR3tbTudDjqtPViWC79vy5YtF5155plv4py/zlr3nCp7PCxI+BXcMeWlrUUc5KFIqQ2d"
    "TufvhoaGHptHeITUL+MSH6cs+2ngcYaO5s9Drh5ZXpCstUhsE1XmdEnhHG62pPp8iYQAS+G7SZLU3JZhUlprHDtn"
    "D5SQVt8aemCw0U0Lr46r+robJObOjhFVCeeMEeXBjaWBIariWxhTwaVUNSxR/QCBWMRCTK+KJ/lrVWtaNYkqx1HV"
    "TLFohpQkybVpmq5mjF3JuTixtlZdd2VVILMMmG/gnL/VWru+fo3a+HDcMdXfxhgoHiKO43Is+NfnLFKW6RGEQ+mQ"
    "yJGM2SCNSKGmjPBPbMjOF9RkJDt8ZHr6oe7KWbPUacqHhe8ihxgjlhmqqxQ05qiYsBWacl5q2w4bbWoRawbMqXeB"
    "+X84+rzmo+kU4oN1CHZJHtDgKBtmHx9IG29TY4BJol5dQGN61mXAIx118qU3OLedlejweanyrDL2JuSX/6/pLy+7"
    "o8HUW6RwZ3NpBNRVi0C/394oo41EYI6phnrXRlBu7XdabfmPSy6ZeT+6HJZh4tI0DY/H0IXs7e2bl96r4vbbpXLP"
    "QMImFS0ymSMPAgVISWjSkfQaSFhPQZmOdrqJ6MPjdPZfrLjwmvacvIz1AAQ4zi6Yenf7s0uZisf/UAo+7BP8fE93"
    "X3fYW3boWM2bhijN/G5ZtdJ7Ha0//mem3jd109jdnOV/wgW9iCdCet6a8VCG3YX4swyl9BmeBICYMsa/oVsjfzvy"
    "0u3/5vu5IPHTL8EsFc7IuElCFAi4a/LZegUTBDCZZYEX1dsSz0P+wbSW3meMOc05d3Lph4cZ4r8ghAA2fqNSCg2K"
    "av0lsGB7rp+65YTXNMTM+RiOnwCUYgNPomU8l+2J457x/qPYGt8wtSo5zNj4fdN3nfAS4dLLbN5CPNNb5zgG0BJp"
    "o07ePOafB47f2MH3AXFbt44Ye9YXWul3V7xcTzdez5lQ1mWlRhczsi1HqmntwFEfX3r2Nx8J10F3M4hej+9/rbUW"
    "2aJ4gKq+HnBZwHUxWFZ7haYIxgHp+17n3P+1tjhnenrmsjhunGatWU7ERsFznaMpzvnuNE2/G0XyC3Ecf4kx5jHg"
    "81keXd+4of/UTj8E94hzLAK+JtRqwNZkVc37BbUy/GOM2WSMGUJ13KoScPjYF/L+9gIxkOrYe7U2763BsX11VCD9"
    "cWNQ3mjh62YPtVvuH1Hryz+AZTzHNzUDwJ/o4dq178my7Crk+lQdLKX0Tx+idI/UM72UUtvTNL3COcQjPeA0NMKD"
    "X8FR0UiinQusSzkut8EYvRNQoZpVraX0BctuK79r16xZEzorMHaNc+52Y8xrtda/yjk/xTk3XKKyMmPMOGPsFinl"
    "PykVFKeiKD6vtW6UFQ0gBzEPXF9om6OEvqeiKL7pnPtAMOF9+eiyQ6AvprW3bD13cFSik9qx+nTaZuNMuw5QoA7R"
    "Ti4ZbwmVSdp2Zz2t/xHqzDTEP7jpweVO6UxF8HWhBpRC1oKajtR99XP3X2tG6m/vnk6ujHIqfNsKowD0EkYQK+zo"
    "DrVMTHsB0sdWqkD4P57721996+ff/w5uzPHc6haHpU6ILfiyESIT8tFvRmd85Ti2vXIFL0hVvietc4y9YNdHaa37"
    "WPv8o8+z7YlXclIv4Yk+lnE3iIKShpHRLJo2qXxUpcWXXDJ8zd6nrbjx+ONv9zykyojoO3+pZO3+yLbbjt1whJu8"
    "sJhRrxE0cC4X7SNZpAe8j1pJZ6TIrZF7dUfcw8Twf06qFZ87+qKv7EKx4fmq4/prQYgAkMZ2vatz27INerr4Nafo"
    "VSwxy5yxDXS+Y8XAdKblHpqxX20OJ7fV3XpVkUbG9t1KRC/Nblz67DzVa7hwl7JYHy9F0XRWcrSELrRrFVps17qx"
    "WY42P7Gz9epbT73kyrKDexCY/fc6o/jTbNpMCa5aRWFjayRnWnFqiVg32O3+Pvfvk/0RWrTClVMWUMTvoDr9iGkO"
    "UvhHff4DJ9Md4PhQXHK+91FPyTn8zPs5cK7/rXNbpB+eYIlUrxH/arfbJ7Raree0Wq3nTU1NPd05t7T2ua8MQoct"
    "sSfEWKokw+7fWzdEe28++bjp2+gM92X1LHfL2Mo9t52yYtOmtcn+jjuYc2//zNpm+5ax47MvDqzMvjR6Znbb2Bmd"
    "2+KnTtzyslBKpHacOwheg8B19XobytpvWXr0xJfp5PFbkxMe+eqLjtiyZQtMqIXHh7hN7Tpbt26IWvcsOTa7VZ3u"
    "bl/2TIyztWnJsddd99b4UOc+SxVW5dDuUVWSokLmzCttqgSr+T73g6zqv8xHq0KF00M+bjPZ/iYtYSzEy5a6C10P"
    "Ftw8x3UbCfWPxbvW5rEYqnXpJl3MiWf0fj7v+iwwjl6saX0w84xjgXEtuHb7O8eP8ljkba3unYdvPHWo16mt44KW"
    "1/7WdqFq0fubU3/XxQN9foD5PK55Hyz5Kq8lvLWHyjyY/v2+X8ZRdpJb8Fqh211/qfEacvDAKLIDMS4grQ50jnnP"
    "W0sc3N85PI+A6+UAc5133quD++ggvmcP9tz+ONzDdQuvX1kO3tej3c+1/bx+VHPf777CWOfhv/30/wHeIkXlsqSF"
    "VQAAAABJRU5ErkJggg=="
)

# Terminal Investor T logo icon (80x80) for email headers — extracted from favicon and resized
# Using this smaller icon version instead of full base64 PNG for better email client compatibility
_LOGO_EMAIL_ICON = (
    "data:image/png;base64,"
    "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"
    "CWbrR334P++UY0QFh80SAAAAAElFTkSuQmCC"
)



_SMTP_PLACEHOLDERS = {"", "REPLACE_ME", "your-email@gmail.com", "replace_me"}


def _smtp_configured() -> bool:
    """Return True only when real (non-placeholder) SMTP credentials are present."""
    gmail_from     = os.environ.get("GMAIL_FROM", "").strip()
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    return (
        bool(gmail_from)
        and bool(gmail_password)
        and gmail_from.lower()     not in _SMTP_PLACEHOLDERS
        and gmail_password.lower() not in _SMTP_PLACEHOLDERS
        and "@" in gmail_from
    )


def _send_email(to_email: str, subject: str, html_body: str) -> bool:
    """Send an HTML email via Gmail SMTP. Returns True on success, False on failure."""
    gmail_from     = os.environ.get("GMAIL_FROM", "").strip()
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()

    if not _smtp_configured():
        logger.warning(
            "SMTP not configured (GMAIL_FROM / GMAIL_APP_PASSWORD are placeholders) "
            "— skipping email to %s. Set real credentials in .env to enable delivery.",
            to_email,
        )
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Terminal Investor <{gmail_from}>"
    msg["To"]      = to_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        # Get custom SMTP settings from .env, fall back to Gmail defaults
        smtp_server = os.environ.get("GMAIL_SMTP_SERVER", "smtp.gmail.com")
        smtp_port = int(os.environ.get("GMAIL_SMTP_PORT", "465"))

        # Use SMTP_SSL for port 465, SMTP with STARTTLS for port 587
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
            server.starttls()

        with server:
            server.login(gmail_from, gmail_password)
            server.sendmail(gmail_from, to_email, msg.as_string())
        logger.info("✉️  Email sent to %s: %s", to_email, subject)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error(
            "SMTP auth failed for %s — check GMAIL_FROM and GMAIL_APP_PASSWORD in .env. "
            "Gmail requires an App Password (not your account password): "
            "https://myaccount.google.com/apppasswords",
            gmail_from,
        )
        return False
    except Exception as exc:
        logger.error("Email send failed to %s: %s", to_email, exc)
        return False


def _send_verification_email(to_email: str, verify_token: str) -> str:
    """Build and send the account verification email. Returns the verify URL."""
    verify_url = f"{_app_base_url()}/auth/verify-email?token={verify_token}"

    # Always log the URL so it's accessible even when SMTP is not configured
    logger.info(
        "🔗 VERIFY URL for %s → %s",
        to_email, verify_url,
    )

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;background:#0d1117;color:#e6edf3;border-radius:12px;overflow:hidden">
      <div style="background:#f0a500;padding:24px 32px;text-align:center">
        <h1 style="margin:0;font-size:2rem;color:#0d1117">🅣 Terminal Investor</h1>
      </div>
      <div style="padding:32px">
        <h2 style="margin-top:0;font-size:1.1rem">Verify your email address</h2>
        <p style="color:#8b949e;line-height:1.6">
          Thanks for signing up! Click the button below to verify your email address
          and activate your 7-day free trial.
        </p>
        <div style="text-align:center;margin:32px 0">
          <a href="{verify_url}"
             style="background:#f0a500;color:#0d1117;padding:14px 32px;border-radius:8px;
                    font-weight:700;font-size:1rem;text-decoration:none;display:inline-block">
            ✅ Verify My Email
          </a>
        </div>
        <p style="color:#8b949e;font-size:.8rem;line-height:1.6">
          Or copy and paste this link into your browser:<br>
          <a href="{verify_url}" style="color:#f0a500;word-break:break-all">{verify_url}</a>
        </p>
        <hr style="border:none;border-top:1px solid #30363d;margin:24px 0">
        <p style="color:#6e7681;font-size:.75rem">
          If you didn't create an account, you can ignore this email safely.
        </p>
      </div>
    </div>
    """
    threading.Thread(
        target=_send_email,
        args=(to_email, "Verify your Terminal Investor account", html),
        daemon=True,
    ).start()

    return verify_url


def _notify_password_changed(to_email: str) -> None:
    """
    Send a security-notification email when a user changes their password
    from the Settings screen.  Fires asynchronously so it never blocks the
    HTTP response.
    """
    from zoneinfo import ZoneInfo
    _TZ_EST    = ZoneInfo("America/New_York")
    now_est    = datetime.now(_TZ_EST)
    tz_label   = "EDT" if now_est.dst() else "EST"
    changed_at = now_est.strftime(f"%B %d, %Y at %H:%M {tz_label}")

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;background:#0d1117;color:#e6edf3;border-radius:12px;overflow:hidden">
      <div style="background:#f0a500;padding:24px 32px;text-align:center">
        <h1 style="margin:0;font-size:2rem;color:#0d1117">🅣 Terminal Investor</h1>
      </div>
      <div style="padding:32px">
        <h2 style="margin-top:0;font-size:1.1rem">Your password was changed</h2>
        <p style="color:#8b949e;line-height:1.6">
          Your Terminal Investor password was successfully updated on
          <strong style="color:#e6edf3">{changed_at}</strong>.
        </p>
        <p style="color:#8b949e;line-height:1.6">
          If you made this change, no action is needed.
        </p>
        <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px 20px;margin:24px 0">
          <p style="margin:0;color:#f85149;font-weight:600;font-size:.9rem">
            ⚠️ If you did NOT make this change
          </p>
          <p style="margin:8px 0 0;color:#8b949e;font-size:.85rem;line-height:1.55">
            Your account may be compromised. Reset your password immediately using
            the <strong style="color:#e6edf3">Forgot Password</strong> link on the sign-in page,
            then contact support.
          </p>
        </div>
        <hr style="border:none;border-top:1px solid #30363d;margin:24px 0">
        <p style="color:#6e7681;font-size:.75rem">
          This is an automated security notification from Terminal Investor.
          Please do not reply to this email.
        </p>
      </div>
    </div>
    """
    threading.Thread(
        target=_send_email,
        args=(to_email, "Your Terminal Investor password was changed", html),
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Strict RFC-5321 email regex — only alphanumerics + . _ % + - in local part.
# Explicitly rejects HTML/script characters (<>{}|) to block stored-XSS via email field.
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9._%+\-]{1,64}"   # local part: safe chars only, max 64
    r"@"
    r"[a-zA-Z0-9.\-]{1,253}"       # domain: alphanumeric + dots + hyphens
    r"\.[a-zA-Z]{2,}$"             # TLD: letters only, at least 2
)


def _extract_token_from_request() -> Optional[str]:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[len("Bearer "):]
    return request.cookies.get("access_token")


def _get_client_ip() -> Optional[str]:
    """Get client IP, validating X-Forwarded-For only from trusted proxies."""
    # Cloudflare header takes precedence
    if request.headers.get("CF-Connecting-IP"):
        return request.headers.get("CF-Connecting-IP")

    # Only trust X-Forwarded-For if from known reverse proxy (Traefik in Docker)
    # Traefik connects from docker network (172.17.x.x)
    if request.remote_addr and request.remote_addr.startswith("172.17."):
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            # Take first IP (original client), log if multiple hops (spoofing attempt)
            ips = [ip.strip() for ip in forwarded.split(",")]
            if len(ips) > 1:
                logger.warning("Suspicious X-Forwarded-For with multiple hops: %s", forwarded)
            return ips[0]

    # Fallback to direct connection IP
    return request.remote_addr


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def auth_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _extract_token_from_request()
        if not token:
            return jsonify({"error": "Authentication required", "code": "missing_token"}), 401
        try:
            payload = decode_token(token)
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token has expired", "code": "token_expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token", "code": "invalid_token"}), 401

        if payload.get("type") != "access":
            return jsonify({"error": "Invalid token type", "code": "invalid_token"}), 401

        user_id = payload.get("sub")
        user = models.get_user_by_id(user_id)
        if not user:
            return jsonify({"error": "User not found", "code": "user_not_found"}), 401

        g.user_id = user_id
        g.user = user
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    """Legacy decorator: checks app users table + admin_users FK table."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _extract_token_from_request()
        if not token:
            return jsonify({"error": "Authentication required", "code": "missing_token"}), 401
        try:
            payload = decode_token(token)
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token has expired", "code": "token_expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token", "code": "invalid_token"}), 401

        if payload.get("type") != "access":
            return jsonify({"error": "Invalid token type", "code": "invalid_token"}), 401

        user_id = payload.get("sub")
        user = models.get_user_by_id(user_id)
        if not user:
            return jsonify({"error": "User not found", "code": "user_not_found"}), 401

        g.user_id = user_id
        g.user = user

        if not models.is_admin(g.user_id):
            return jsonify({"error": "Admin access required", "code": "forbidden"}), 403

        return fn(*args, **kwargs)
    return wrapper


def admin_portal_required(fn):
    """
    Decorator for admin portal API routes.
    Checks for admin_access token (from admin_access cookie or Authorization header).
    Admin accounts are stored in admin_accounts table — completely separate from app users.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # Try admin_access cookie first, then Authorization header
        token = request.cookies.get("admin_access") or ""
        if not token:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[len("Bearer "):]
        if not token:
            return jsonify({"error": "Admin authentication required", "code": "missing_token"}), 401
        try:
            payload = decode_token(token)
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Admin session expired — please log in again", "code": "token_expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid admin token", "code": "invalid_token"}), 401

        if payload.get("type") != "admin_access":
            return jsonify({"error": "Invalid token type for admin portal", "code": "invalid_token"}), 401

        admin_id = payload.get("sub")
        admin = models.get_admin_account_by_id(admin_id)
        if not admin:
            return jsonify({"error": "Admin account not found or inactive", "code": "admin_not_found"}), 401

        # Server-side idle timeout — enforces the 15-min inactivity limit regardless of JS state
        now = _time.time()
        last = _admin_last_activity.get(admin_id)
        if last is not None and (now - last) > ADMIN_IDLE_TIMEOUT_SECS:
            _admin_last_activity.pop(admin_id, None)
            return jsonify({"error": "Session expired due to inactivity — please log in again", "code": "idle_timeout"}), 401
        _admin_last_activity[admin_id] = now

        g.admin_id = admin_id
        g.admin = admin
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Blueprint routes
# ---------------------------------------------------------------------------

_login_attempts:    dict = {}  # ip -> [timestamp, ...]
_register_attempts: dict = {}  # ip -> [timestamp, ...]
_forgot_attempts:   dict = {}  # ip -> [timestamp, ...]
_RATE_LIMIT_WINDOW = 900   # 15 minutes  — login brute-force window
_RATE_LIMIT_MAX    = 10    # max failed login attempts per window
_REGISTER_WINDOW   = 60    # 1 minute    — register: 5/minute (strict, matches @limiter.limit("5/minute"))
_REGISTER_MAX      = 5     # max registration attempts per 1-min window per IP
_FORGOT_WINDOW     = 900   # 15 minutes  — forgot-password: 3/15 min (email quota protection)
_FORGOT_MAX        = 3     # max forgot-password attempts per 15-min window per IP

def _check_rate_limit(ip: str) -> bool:
    """Return True if the IP is NOT rate-limited. Cleans up old entries."""
    now = _time.time()
    attempts = _login_attempts.get(ip, [])
    # Remove attempts outside the window
    attempts = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) < _RATE_LIMIT_MAX

def _record_failed_login(ip: str) -> None:
    now = _time.time()
    attempts = _login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW]
    attempts.append(now)
    _login_attempts[ip] = attempts

def _check_and_record_endpoint_limit(store: dict, ip: str, max_calls: int,
                                     window: int = _RATE_LIMIT_WINDOW) -> bool:
    """Return True if the request is ALLOWED (under limit), False if rate-limited.
    Records the current call on every allowed invocation and prunes old timestamps.
    window: sliding time window in seconds (default: _RATE_LIMIT_WINDOW = 15 min).
    """
    now = _time.time()
    calls = store.get(ip, [])
    calls = [t for t in calls if now - t < window]
    if len(calls) >= max_calls:
        store[ip] = calls
        return False
    calls.append(now)
    store[ip] = calls
    return True

auth_bp = Blueprint("auth_bp", __name__, url_prefix="/auth")


@auth_bp.route("/register", methods=["POST"])
def register():
    """POST /auth/register — Create account + trial subscription + send verification email."""
    ip = _get_client_ip()
    if not _check_and_record_endpoint_limit(_register_attempts, ip or "unknown", _REGISTER_MAX, _REGISTER_WINDOW):
        logger.warning("register: rate limit hit for IP %s", ip)
        return jsonify({"error": "Too many registration attempts. Please wait 1 minute before trying again."}), 429

    data = request.get_json(silent=True) or {}
    email     = (data.get("email")     or "").strip().lower()
    password  =  data.get("password",  "")
    full_name = (data.get("full_name") or "").strip() or None

    errors = {}
    if not email or not _EMAIL_RE.match(email):
        errors["email"] = "A valid email address is required"
    if not password or len(password) < 12:
        errors["password"] = "Password must be at least 12 characters"
    if errors:
        return jsonify({"error": "Validation failed", "fields": errors}), 422

    if models.get_user_by_email(email):
        return jsonify({"error": "An account with that email already exists"}), 409

    try:
        password_hash = hash_password(password)
        user = models.create_user(email, password_hash, full_name)
    except Exception as exc:
        # Catch UNIQUE constraint violation from the DB (race condition: two requests
        # passed the get_user_by_email check before either committed the new row).
        # Treat it the same as the explicit 409 check above — no email is sent.
        exc_str = str(exc).lower()
        if "unique" in exc_str or "duplicate" in exc_str:
            logger.warning("register: duplicate email race condition for %s", email)
            return jsonify({"error": "An account with that email already exists"}), 409
        logger.error("register: create_user failed: %s", exc)
        return jsonify({"error": "Failed to create account"}), 500

    user_id = str(user["id"])

    # Store initial password in history
    try:
        models.store_password_in_history(user_id, password_hash)
    except Exception as exc:
        logger.error("register: store password history failed: %s", exc)

    # Create active subscription (no trial period)
    try:
        models.create_subscription(
            user_id=user_id,
            plan_id=1,
            status="active",
        )
    except Exception as exc:
        logger.error("register: create_subscription failed: %s", exc)

    # Generate + store verification token, send email asynchronously
    verify_token = secrets.token_urlsafe(32)
    verify_url   = None
    try:
        models.set_email_verify_token(user_id, verify_token)
        verify_url = _send_verification_email(email, verify_token)
        logger.info("Verification email dispatched for user %s", user_id)
    except Exception as exc:
        logger.error("register: send verification email failed: %s", exc)

    tokens = generate_tokens(user_id)
    # Store refresh token hash for rotation validation on next /auth/refresh call
    try:
        models.store_refresh_token_hash(user_id, _hash_token(tokens["refresh_token"]))
    except Exception as exc:
        logger.error("register: store refresh token hash failed: %s", exc)

    models.log_audit(
        user_id=user_id,
        action="user_registered",
        details_dict={"email": email},
        ip_address=_get_client_ip(),
    )

    resp_body: dict = {
        "user_id": user_id,
        "email":   user["email"],
        "email_verification_sent": True,
    }
    # When SMTP is not configured, surface the verify URL so the client can
    # present it directly (useful during local development / testing).
    if not _smtp_configured() and verify_url:
        resp_body["verify_url"] = verify_url

    resp = jsonify(resp_body)
    # Set HttpOnly cookies so the user is immediately logged in after registration
    resp.set_cookie("access_token",  tokens["access_token"],
                    httponly=True, samesite="Lax", secure=True,
                    max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    resp.set_cookie("refresh_token", tokens["refresh_token"],
                    httponly=True, samesite="Lax", secure=True,
                    max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600)
    return resp, 201


@auth_bp.route("/login", methods=["POST"])
def login():
    """POST /auth/login — Authenticate and return tokens with account lockout protection."""
    data     = request.get_json(silent=True) or {}
    email    = (data.get("email")    or "").strip().lower()
    password =  data.get("password", "")

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 422

    ip = _get_client_ip()

    # Check if IP is rate limited (defense-in-depth: IP limit + database-based account lockout)
    if not _check_rate_limit(ip or "unknown"):
        logger.warning("Login rate limit exceeded for IP: %s", ip or "unknown")
        return jsonify({"error": "Too many login attempts. Please wait 15 minutes."}), 429

    # Check if account is locked due to failed attempts
    if models.is_account_locked(email, max_attempts=5, lockout_minutes=5):
        failed_count = models.get_failed_login_attempts(email, 5)
        return jsonify({
            "error": "Account temporarily locked due to too many failed login attempts. Please try again in 5 minutes.",
            "code": "account_locked",
            "failed_attempts": failed_count
        }), 429

    # Verify credentials
    user = models.get_user_by_email(email)
    if not user or not check_password(password, user["password_hash"]):
        # Record failed attempt
        models.record_login_attempt(email, ip or "unknown", success=False, user_id=str(user["id"]) if user else None)
        return jsonify({"error": "Invalid email or password"}), 401

    # Login successful - clear failed attempts
    models.clear_failed_login_attempts(email)
    models.record_login_attempt(email, ip or "unknown", success=True, user_id=str(user["id"]))
    models.update_user_last_login(str(user["id"]), ip)

    # Gate login behind email verification
    if not user.get("email_verified"):
        return jsonify({
            "error": "Please verify your email address before logging in. Check your inbox for the verification link.",
            "code": "email_not_verified"
        }), 403

    subscription = models.get_user_subscription(str(user["id"]))
    sub_info = None
    if subscription:
        sub_info = {
            "status":        subscription.get("status"),
            "plan_name":     subscription.get("plan_name"),
            "trial_ends_at": _serialize_dt(subscription.get("trial_ends_at")),
            "expires_at":    _serialize_dt(subscription.get("expires_at")),
        }

    # Check subscription status — block access for inactive/cancelled/pending accounts
    if subscription:
        sub_status = subscription.get("status", "").lower()
        if sub_status == "inactive":
            return jsonify({
                "error": "Your account is inactive. Please contact the helpdesk to reactivate it.",
                "code": "account_inactive"
            }), 403
        elif sub_status == "cancelled":
            return jsonify({
                "error": "Your subscription is cancelled. Please contact the helpdesk to renew it.",
                "code": "subscription_cancelled"
            }), 403
        elif sub_status == "pending_payment":
            return jsonify({
                "error": "Your account is pending payment. Please contact the helpdesk to complete your payment.",
                "code": "pending_payment"
            }), 403

    tokens = generate_tokens(str(user["id"]))
    # Store refresh token hash for rotation validation on next /auth/refresh call
    try:
        models.store_refresh_token_hash(str(user["id"]), _hash_token(tokens["refresh_token"]))
    except Exception as exc:
        logger.error("login: store refresh token hash failed: %s", exc)

    models.log_audit(
        user_id=str(user["id"]),
        action="user_login",
        ip_address=ip,
    )

    resp = jsonify({
        "user_id":      str(user["id"]),
        "email":        user["email"],
        "subscription": sub_info,
    })
    # Set HttpOnly cookies — more secure than localStorage
    resp.set_cookie("access_token",  tokens["access_token"],
                    httponly=True, samesite="Lax", secure=True,
                    max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    resp.set_cookie("refresh_token", tokens["refresh_token"],
                    httponly=True, samesite="Lax", secure=True,
                    max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600)
    return resp


@auth_bp.route("/logout", methods=["POST"])
def logout():
    """POST /auth/logout — Clear cookies and invalidate refresh token hash."""
    # Invalidate the stored refresh token hash so the cookie (still held by
    # the browser until it expires) cannot be replayed after logout.
    token = request.cookies.get("refresh_token", "")
    if token:
        try:
            payload = decode_token(token)
            user_id = payload.get("sub")
            if user_id:
                models.store_refresh_token_hash(user_id, "")
        except Exception:
            pass  # expired or invalid token — nothing to invalidate

    response = jsonify({"ok": True})
    response.delete_cookie("access_token",  path="/", samesite="Lax", secure=True)
    response.delete_cookie("refresh_token", path="/", samesite="Lax", secure=True)
    return response


@auth_bp.route("/forgot-password", methods=["POST"])
def forgot_password():
    """POST /auth/forgot-password — Send password reset link via email."""
    ip = _get_client_ip()
    if not _check_and_record_endpoint_limit(_forgot_attempts, ip or "unknown", _FORGOT_MAX, _FORGOT_WINDOW):
        logger.warning("forgot_password: rate limit hit for IP %s", ip)
        return jsonify({"error": "Too many password reset requests. Please wait 15 minutes before trying again."}), 429

    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()

    if not email:
        return jsonify({"error": "Email address is required"}), 422

    user = models.get_user_by_email(email)
    if not user:
        # For security, don't reveal whether email exists
        return jsonify({"message": "If an account exists, a reset link has been sent"}), 200

    # Generate a secure token valid for 24 hours
    reset_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(tz=timezone.utc) + timedelta(hours=24)

    models.set_password_reset_token(str(user["id"]), reset_token, expires_at)

    # Build reset link
    reset_link = f"{_app_base_url()}/?reset_token={reset_token}&email={email}"

    # Send email in background
    def send_reset_email():
        try:
            reset_html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;background:#0d1117;color:#e6edf3;border-radius:12px;overflow:hidden">
      <div style="background:#f0a500;padding:24px 32px;text-align:center">
        <h1 style="margin:0;font-size:2rem;color:#0d1117">🅣 Terminal Investor</h1>
      </div>
      <div style="padding:32px">
        <h2 style="margin-top:0;font-size:1.1rem">Reset your password</h2>
        <p style="color:#8b949e;line-height:1.6">
          We received a request to reset the password for your Terminal Investor account.
          Click the button below to choose a new password.
        </p>
        <div style="text-align:center;margin:32px 0">
          <a href="{reset_link}"
             style="background:#f0a500;color:#0d1117;padding:14px 32px;border-radius:8px;
                    font-weight:700;font-size:1rem;text-decoration:none;display:inline-block">
            Reset My Password
          </a>
        </div>
        <p style="color:#8b949e;font-size:.8rem;line-height:1.6">
          Or copy and paste this link into your browser:<br>
          <a href="{reset_link}" style="color:#f0a500;word-break:break-all">{reset_link}</a>
        </p>
        <hr style="border:none;border-top:1px solid #30363d;margin:24px 0">
        <p style="color:#6e7681;font-size:.75rem">
          This link will expire in 24 hours. If you didn't request a password reset,
          you can safely ignore this email — your password will not change.
        </p>
      </div>
    </div>
            """
            _send_email(email, "Reset Your Terminal Investor Password", reset_html)
        except Exception as e:
            logger.error(f"Failed to send password reset email to {email}: {e}")

    threading.Thread(target=send_reset_email, daemon=True).start()

    # Always return success to prevent email enumeration attacks
    return jsonify({"message": "If an account exists, a reset link has been sent"}), 200


@auth_bp.route("/reset-password", methods=["POST"])
def reset_password():
    """POST /auth/reset-password — Reset password using reset token with policy enforcement."""
    data = request.get_json(silent=True) or {}
    reset_token = (data.get("reset_token") or "").strip()
    password = data.get("password", "")

    if not reset_token or not password:
        return jsonify({"error": "Reset token and password are required"}), 422

    # Validate password strength (minimum 12 characters)
    if len(password) < 12:
        return jsonify({"error": "Password must be at least 12 characters"}), 422

    # Validate token
    user = models.get_user_by_reset_token(reset_token)
    if not user:
        return jsonify({"error": "Invalid or expired reset link"}), 401

    # Check password history - user cannot reuse last 3 passwords
    old_password_hashes = models.get_password_history(str(user["id"]), limit=3)
    for old_hash in old_password_hashes:
        if check_password(password, old_hash):
            return jsonify({
                "error": "You cannot reuse one of your last 3 passwords. Please choose a different password."
            }), 422

    # Hash and update password
    password_hash = hash_password(password)

    # Store old password in history before updating
    models.store_password_in_history(str(user["id"]), user["password_hash"])

    with models.db_cursor() as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s, email_verified = TRUE, email_verify_token = NULL WHERE id = %s",
            (password_hash, str(user["id"])),
        )

    # Clear the reset token
    models.clear_password_reset_token(str(user["id"]))

    # Log the password change
    models.log_audit(
        user_id=str(user["id"]),
        action="password_reset",
    )

    return jsonify({"message": "Password updated successfully"}), 200


@auth_bp.route("/verify-reset-token", methods=["GET"])
def verify_reset_token():
    """GET /auth/verify-reset-token?token=... — Verify if a reset token is valid."""
    token = request.args.get("token", "").strip()

    if not token:
        return jsonify({"valid": False}), 200

    user = models.get_user_by_reset_token(token)
    return jsonify({"valid": user is not None}), 200


@auth_bp.route("/refresh", methods=["POST"])
def refresh():
    """POST /auth/refresh — Rotate refresh token and issue a new access token.

    Implements refresh-token rotation per OWASP ASVS 3.3:
      1. Verify the incoming refresh JWT is valid and not expired.
      2. Hash the token and compare against the stored hash in DB.
         - Mismatch = already-rotated token (potential theft) → reject + clear hash.
      3. Issue brand-new access + refresh tokens, persist new hash.
      4. Return both tokens via HttpOnly cookies.
    """
    data          = request.get_json(silent=True) or {}
    # Accept refresh token from either body or HttpOnly cookie
    refresh_token = data.get("refresh_token", "").strip() or request.cookies.get("refresh_token", "")

    if not refresh_token:
        return jsonify({"error": "refresh_token is required"}), 422

    try:
        payload = decode_token(refresh_token)
    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Refresh token has expired", "code": "token_expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid refresh token", "code": "invalid_token"}), 401

    if payload.get("type") != "refresh":
        return jsonify({"error": "Invalid token type", "code": "invalid_token"}), 401

    user_id = payload.get("sub")
    if not models.get_user_by_id(user_id):
        return jsonify({"error": "User not found"}), 401

    # ── Rotation check ────────────────────────────────────────────────
    incoming_hash = _hash_token(refresh_token)
    try:
        valid = models.verify_and_rotate_refresh_token(user_id, incoming_hash)
    except Exception as exc:
        logger.error("refresh: rotation DB check failed for %s: %s", user_id, exc)
        return jsonify({"error": "Session verification failed — please sign in again", "code": "db_error"}), 401

    if not valid:
        # Token already rotated — possible replay/theft. Clear stored hash so
        # all subsequent refresh attempts also fail until the user re-logs in.
        try:
            models.store_refresh_token_hash(user_id, "")
        except Exception:
            pass
        logger.warning(
            "refresh: rotated-token reuse detected for user %s (possible token theft) "
            "— session invalidated", user_id[:8]
        )
        models.log_audit(
            user_id=user_id,
            action="refresh_token_reuse_detected",
            details_dict={"note": "Rotated refresh token reused — session invalidated"},
            ip_address=_get_client_ip(),
        )
        return jsonify({"error": "Session invalid — please sign in again", "code": "token_reused"}), 401

    # ── Issue new token pair ──────────────────────────────────────────
    new_tokens = generate_tokens(user_id)
    try:
        models.store_refresh_token_hash(user_id, _hash_token(new_tokens["refresh_token"]))
    except Exception as exc:
        logger.error("refresh: store new refresh token hash failed: %s", exc)

    resp = jsonify({"access_token": new_tokens["access_token"]})
    resp.set_cookie("access_token",  new_tokens["access_token"],
                    httponly=True, samesite="Lax", secure=True,
                    max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    resp.set_cookie("refresh_token", new_tokens["refresh_token"],
                    httponly=True, samesite="Lax", secure=True,
                    max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600)
    return resp


@auth_bp.route("/me", methods=["GET"])
@auth_required
def me():
    """GET /auth/me — Return current user profile, subscription, plan features, and admin flag."""
    user         = g.user
    subscription = models.get_user_subscription(g.user_id)
    is_admin     = models.is_admin(g.user_id)

    features = None
    sub_info = None
    if subscription:
        raw_features = subscription.get("features")
        if isinstance(raw_features, str):
            import json as _json
            raw_features = _json.loads(raw_features)
        features = raw_features
        sub_info = {
            "status":          subscription.get("status"),
            "plan_id":         subscription.get("plan_id"),
            "plan_name":       subscription.get("plan_name"),
            "display_name":    subscription.get("display_name"),
            "billing_cycle":   subscription.get("billing_cycle"),
            "started_at":      _serialize_dt(subscription.get("started_at")),
            "expires_at":      _serialize_dt(subscription.get("expires_at")),
            "trial_ends_at":   _serialize_dt(subscription.get("trial_ends_at")),
            "runs_per_day":    subscription.get("runs_per_day"),
            "max_ai_picks":    subscription.get("max_ai_picks"),
            "max_pdf_history": subscription.get("max_pdf_history"),
        }

    return jsonify({
        "user_id":        str(user["id"]),
        "email":          user["email"],
        "full_name":      user.get("full_name"),
        "email_verified": user.get("email_verified"),
        "is_admin":       is_admin,
        "created_at":     _serialize_dt(user.get("created_at")),
        "last_login_at":  _serialize_dt(user.get("last_login_at")),
        "subscription":   sub_info,
        "features":       features,
    })


@auth_bp.route("/verify-email", methods=["GET"])
def verify_email():
    """
    GET /auth/verify-email?token=<token>
    Called when user clicks the link in the verification email.
    Marks email as verified and redirects to the app.
    """
    token = request.args.get("token", "").strip()
    if not token:
        return jsonify({"error": "Verification token is required"}), 422

    user = models.get_user_by_verify_token(token)
    if not user:
        # Token expired or already used — redirect with error param
        return redirect(f"{_app_base_url()}/?verify_error=1")

    if user.get("email_verified"):
        # Already verified — redirect with info
        return redirect(f"{_app_base_url()}/?already_verified=1")

    models.verify_user_email(str(user["id"]))
    models.log_audit(
        user_id=str(user["id"]),
        action="email_verified",
        ip_address=_get_client_ip(),
    )
    logger.info("Email verified for user %s", user["id"])

    # Redirect to app — frontend detects ?verified=1 and shows success toast
    return redirect(f"{_app_base_url()}/?verified=1")


@auth_bp.route("/resend-verification", methods=["POST"])
@auth_required
def resend_verification():
    """POST /auth/resend-verification — Resend the email verification link."""
    user = g.user

    if user.get("email_verified"):
        return jsonify({"message": "Your email is already verified."}), 200

    verify_token = secrets.token_urlsafe(32)
    models.set_email_verify_token(g.user_id, verify_token)
    verify_url = _send_verification_email(user["email"], verify_token)

    models.log_audit(
        user_id=g.user_id,
        action="verification_email_resent",
        ip_address=_get_client_ip(),
    )

    resp: dict = {"ok": True, "message": "Verification email sent. Please check your inbox."}
    if not _smtp_configured() and verify_url:
        resp["verify_url"] = verify_url

    return jsonify(resp), 200


@auth_bp.route("/profile", methods=["POST"])
@auth_required
def update_profile():
    """
    POST /auth/profile — Update mutable user profile fields.
    Body: { "full_name": "Jane Doe", "email": "new@example.com", "password": "newpass123" }
    """
    data      = request.get_json(silent=True) or {}
    full_name = (data.get("full_name") or "").strip() or None
    email     = (data.get("email") or "").strip().lower() or None
    new_pw    = data.get("password", "").strip() or None

    updates = {}

    if full_name:
        models.update_user_profile(g.user_id, full_name=full_name)
        updates["full_name"] = full_name

    if email:
        # Validate email format
        if not _EMAIL_RE.match(email):
            return jsonify({"error": "Invalid email format"}), 422
        # Check if email already exists
        existing = models.get_user_by_email(email)
        if existing and str(existing["id"]) != g.user_id:
            return jsonify({"error": "Email already in use"}), 409
        models.update_user_profile(g.user_id, email=email)
        updates["email"] = email

    if new_pw:
        # ── Password policy: same rules as signup and reset-password ───────
        if len(new_pw) < 12:
            return jsonify({"error": "Password must be at least 12 characters"}), 422

        # ── Reuse prevention: block last 3 passwords ────────────────────────
        old_hashes = models.get_password_history(g.user_id, limit=3)
        for old_hash in old_hashes:
            if check_password(new_pw, old_hash):
                return jsonify({
                    "error": "You cannot reuse one of your last 3 passwords. Please choose a different password."
                }), 422

        # ── Persist: store current hash in history, then update ─────────────
        current_user = models.get_user_by_id(g.user_id)
        if current_user and current_user.get("password_hash"):
            models.store_password_in_history(g.user_id, current_user["password_hash"])

        pw_hash = hash_password(new_pw)
        with models.db_cursor() as cur:
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (pw_hash, g.user_id),
            )
        updates["password"] = "***"  # Don't send actual password back

        # ── Security email: notify user their password was changed ───────────
        user_for_email = models.get_user_by_id(g.user_id)
        if user_for_email and user_for_email.get("email"):
            _notify_password_changed(user_for_email["email"])

    if updates:
        models.log_audit(
            user_id=g.user_id,
            action="profile_updated",
            details_dict=updates,
        )

    # Return updated user object
    user = models.get_user_by_id(g.user_id)
    return jsonify({
        "ok":       True,
        "full_name": user.get("full_name") if user else full_name,
        "email":    user.get("email") if user else email,
    })


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------

def _serialize_dt(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
