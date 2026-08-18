[app]
title = Offline Group Tracker
package.name = offlinetracker
package.domain = org.offlinetracker

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json,txt

version = 0.1.0

requirements = python3,kivy==2.3.0,plyer,pyjnius

# Pin the Android-target Python version explicitly. Without this,
# python-for-android can resolve a newer Python than Kivy 2.3.0's
# generated Cython C code supports (e.g. CPython 3.13/3.14 changed the
# _PyLong_AsByteArray() C API signature, breaking Kivy-SDL2 compilation).
# 3.11 is a known-good match for kivy==2.3.0 + cython==0.29.36.
p4a.python_version = 3.11

orientation = portrait
fullscreen = 0

icon.filename = %(source.dir)s/icon.png

# ---------------------------------------------------------------------------
# ANDROID PERMISSIONS
# Requested here AND at runtime (see request_android_permissions() in main.py)
# because Android 6+ requires a runtime prompt in addition to the manifest
# entry for "dangerous" permissions.
# ---------------------------------------------------------------------------
android.permissions = \
    ACCESS_FINE_LOCATION, \
    ACCESS_COARSE_LOCATION, \
    ACCESS_BACKGROUND_LOCATION, \
    BLUETOOTH, \
    BLUETOOTH_ADMIN, \
    BLUETOOTH_SCAN, \
    BLUETOOTH_CONNECT, \
    BLUETOOTH_ADVERTISE, \
    ACCESS_WIFI_STATE, \
    CHANGE_WIFI_STATE, \
    ACCESS_NETWORK_STATE, \
    CHANGE_NETWORK_STATE, \
    NEARBY_WIFI_DEVICES, \
    FOREGROUND_SERVICE, \
    FOREGROUND_SERVICE_LOCATION, \
    WAKE_LOCK, \
    POST_NOTIFICATIONS, \
    INTERNET

# Android API levels
android.api = 34
android.minapi = 24
android.ndk = 25b
android.archs = arm64-v8a, armeabi-v7a

# Foreground service is required to keep GPS + mesh alive with screen locked.
android.foreground_service = 1

android.allow_backup = 1

[buildozer]
log_level = 2
warn_on_root = 1
