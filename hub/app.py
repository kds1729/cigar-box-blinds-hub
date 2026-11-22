# Copyright (C) 2026 Keith Sprochi
# SPDX-License-Identifier: GPL-2.0-or-later
"""Blinds hub: web UI + REST API + cron-style scheduler, talking to the antenna Nano
over USB serial via controller.BlindsController.

Run: .venv/bin/python hub/app.py --port /dev/ttyUSB0 [--http-port 8420] [--host ::]
"""
import argparse
import json
import os

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify, request, render_template

from controller import BlindsController

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEDULE_PATH = os.path.join(HERE, "schedule.json")

app = Flask(__name__)
ctrl = None  # set in main()
scheduler = BackgroundScheduler()

DAY_MAP = {"mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu", "fri": "fri", "sat": "sat", "sun": "sun"}


def _load_schedule():
    if not os.path.exists(SCHEDULE_PATH):
        return []
    with open(SCHEDULE_PATH) as f:
        return json.load(f)


def _save_schedule(rules):
    tmp = SCHEDULE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rules, f, indent=2)
    os.replace(tmp, SCHEDULE_PATH)


def _run_rule(rule):
    room = rule["room"]
    action = rule["action"]
    try:
        if action == "position":
            ctrl.set_position(room, rule["percent"])
        elif action == "up":
            ctrl.up(room)
        elif action == "down":
            ctrl.down(room)
        elif action == "stop":
            ctrl.stop(room)
    except Exception as e:
        app.logger.error("schedule rule %s failed: %s", rule.get("id"), e)


def _add_job(rule):
    hh, mm = rule["time"].split(":")
    days = ",".join(DAY_MAP[d] for d in rule["days"])
    trigger = CronTrigger(day_of_week=days, hour=int(hh), minute=int(mm))
    scheduler.add_job(_run_rule, trigger, args=[rule], id=rule["id"], replace_existing=True)


def _reload_jobs():
    scheduler.remove_all_jobs()
    for rule in _load_schedule():
        if rule.get("enabled", True):
            _add_job(rule)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/rooms")
def api_rooms():
    return jsonify(ctrl.get_rooms())


def _room_or_error(room):
    if room not in ctrl.rooms:
        return jsonify({"error": "unknown room"}), 404
    if not ctrl.rooms[room].get("captured"):
        return jsonify({"error": f"{room} has no captured protocol data yet"}), 400
    return None


@app.route("/api/rooms/<room>/command", methods=["POST"])
def api_command(room):
    err = _room_or_error(room)
    if err:
        return err
    cmd = request.get_json(force=True).get("cmd")
    if cmd not in ("up", "down", "stop"):
        return jsonify({"error": "cmd must be up, down, or stop"}), 400
    try:
        return jsonify(getattr(ctrl, cmd)(room))
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


@app.route("/api/rooms/<room>/position", methods=["POST"])
def api_set_position(room):
    """Send the blind a set-position command. Doesn't wait or assume it arrived; GET
    this endpoint afterwards to read where the blind actually is."""
    err = _room_or_error(room)
    if err:
        return err
    pct = request.get_json(force=True).get("percent")
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        pct = -1
    if not 0 <= pct <= 100:
        return jsonify({"error": "percent must be 0-100"}), 400
    try:
        return jsonify(ctrl.set_position(room, pct))
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


@app.route("/api/rooms/<room>/position", methods=["GET"])
def api_read_position(room):
    """Query the blind and return the position it reports."""
    err = _room_or_error(room)
    if err:
        return err
    try:
        return jsonify(ctrl.read_position(room))
    except Exception as e:
        return jsonify({"error": str(e)}), 504


@app.route("/api/rooms/<room>/limit/<kind>", methods=["POST"])
def api_set_limit(room, kind):
    """Make the blind's current (freshly read) position its bottom or top line."""
    err = _room_or_error(room)
    if err:
        return err
    if kind not in ctrl.LIMIT_KEYS:
        return jsonify({"error": "kind must be bottom or top"}), 400
    try:
        return jsonify(ctrl.set_limit_here(room, kind))
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 504


@app.route("/api/rooms/<room>/limit/<kind>", methods=["DELETE"])
def api_clear_limit(room, kind):
    err = _room_or_error(room)
    if err:
        return err
    if kind not in ctrl.LIMIT_KEYS:
        return jsonify({"error": "kind must be bottom or top"}), 400
    ctrl.clear_limit(room, kind)
    return jsonify({"ok": True})


@app.route("/api/schedule", methods=["GET"])
def api_schedule_list():
    return jsonify(_load_schedule())


@app.route("/api/schedule", methods=["POST"])
def api_schedule_add():
    body = request.get_json(force=True)
    rules = _load_schedule()
    new_id = str(max([int(r["id"]) for r in rules], default=0) + 1)
    rule = {
        "id": new_id,
        "room": body["room"],
        "days": body["days"],          # list of mon/tue/wed/thu/fri/sat/sun
        "time": body["time"],          # "HH:MM", 24h
        "action": body["action"],      # up/down/stop/position
        "percent": body.get("percent"),
        "enabled": body.get("enabled", True),
    }
    rules.append(rule)
    _save_schedule(rules)
    _reload_jobs()
    return jsonify(rule)


@app.route("/api/schedule/<rule_id>", methods=["PUT"])
def api_schedule_update(rule_id):
    body = request.get_json(force=True)
    rules = _load_schedule()
    for r in rules:
        if r["id"] == rule_id:
            r.update(body)
            break
    else:
        return jsonify({"error": "not found"}), 404
    _save_schedule(rules)
    _reload_jobs()
    return jsonify({"ok": True})


@app.route("/api/schedule/<rule_id>", methods=["DELETE"])
def api_schedule_delete(rule_id):
    rules = [r for r in _load_schedule() if r["id"] != rule_id]
    _save_schedule(rules)
    _reload_jobs()
    return jsonify({"ok": True})


def main():
    global ctrl
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyUSB0", help="the Nano's serial port (macOS: /dev/cu.usbserial-*)")
    ap.add_argument("--http-port", type=int, default=8420)
    # "::" listens on IPv6 and (on Linux, dual-stack) IPv4 too. Needed when
    # clients reach the hub as blinds.local, which resolves to IPv6 first.
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    ctrl = BlindsController(args.port)
    _reload_jobs()
    scheduler.start()
    app.run(host=args.host, port=args.http_port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
