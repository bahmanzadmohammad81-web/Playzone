import os, sys, json, time, hmac, hashlib, tempfile, urllib.parse, unittest
from unittest import mock

TMP = tempfile.mkdtemp()
os.environ.update(BOT_TOKEN="123:TEST", DB_PATH=os.path.join(TMP, "t.db"),
                  WEBHOOK_SECRET="hook", APP_URL="https://example.com")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import app as A  # noqa: E402


def init_data(uid, name="Ali", auth=None, token="123:TEST"):
    fields = {"auth_date": str(auth or int(time.time())), "query_id": "AAA",
              "user": json.dumps({"id": uid, "first_name": name, "username": f"u{uid}"},
                                 separators=(",", ":"), ensure_ascii=False)}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields) + "&hash=" + h


class PlayZoneTests(unittest.TestCase):
    def setUp(self):
        self.c = A.app.test_client()

    def hdr(self, uid=1, **kw):
        return {"X-Init-Data": init_data(uid, **kw)}

    def start(self, game, uid=1):
        r = self.c.post("/api/session", json={"game": game}, headers=self.hdr(uid))
        self.assertEqual(r.status_code, 200)
        return r.get_json()["token"]

    def test_rejects_missing_and_forged_auth(self):
        self.assertEqual(self.c.get("/api/home").status_code, 401)
        forged = init_data(5).replace("hash=", "hash=00")
        self.assertEqual(self.c.get("/api/home", headers={"X-Init-Data": forged}).status_code, 401)

    def test_rejects_expired_auth(self):
        r = self.c.get("/api/home", headers=self.hdr(2, auth=int(time.time()) - 90000))
        self.assertEqual(r.status_code, 401)

    def test_valid_user_gets_home(self):
        r = self.c.get("/api/home", headers=self.hdr(3, name="Sara"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["me"]["level"], 1)

    def submit(self, uid, game, value, later=10):
        tok = self.start(game, uid=uid)
        with mock.patch.object(A.time, "time", return_value=time.time() + later):
            return tok, self.c.post("/api/score", json={"token": tok, "value": value}, headers=self.hdr(uid))

    def test_snake_flow_single_use_and_ranking(self):
        with mock.patch.object(A, "MIN_PLAY_SECONDS", 0):
            tok, r = self.submit(10, "snake", 12)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["points"], 12)
            again = self.c.post("/api/score", json={"token": tok, "value": 12}, headers=self.hdr(10))
            self.assertEqual(again.status_code, 403)  # duplicate submission blocked

    def test_token_belongs_to_user(self):
        tok = self.start("snake", uid=11)
        r = self.c.post("/api/score", json={"token": tok, "value": 5}, headers=self.hdr(12))
        self.assertEqual(r.status_code, 403)

    def test_ranking_and_my_rank(self):
        self.submit(40, "snake", 30)
        self.submit(41, "snake", 5)
        data = self.c.get("/api/ranking?game=snake&period=all", headers=self.hdr(41)).get_json()
        self.assertEqual(data["top"][0]["value"], 30)
        self.assertEqual(data["me"]["rank"], 2)
        self.assertTrue(data["top"][1]["is_me"])

    def test_reaction_rejects_impossible_value(self):
        with mock.patch.object(A, "MIN_PLAY_SECONDS", 0):
            tok = self.start("reaction", uid=20)
            with mock.patch.object(A.time, "time", return_value=time.time() + 10):
                r = self.c.post("/api/score", json={"token": tok, "value": 10}, headers=self.hdr(20))
            self.assertEqual(r.status_code, 422)

    def test_math_rejects_over_max(self):
        tok = self.start("math", uid=21)
        with mock.patch.object(A.time, "time", return_value=time.time() + 30):
            r = self.c.post("/api/score", json={"token": tok, "value": 500}, headers=self.hdr(21))
        self.assertEqual(r.status_code, 422)

    def test_too_fast_submission_rejected(self):
        tok = self.start("snake", uid=22)
        r = self.c.post("/api/score", json={"token": tok, "value": 3}, headers=self.hdr(22))
        self.assertEqual(r.status_code, 422)

    def test_webhook_secret_and_commands(self):
        self.assertEqual(self.c.post("/webhook/wrong", json={}).status_code, 403)
        update = {"message": {"text": "/start", "from": {"id": 30, "first_name": "Reza"}, "chat": {"id": 30}}}
        with mock.patch.object(A.requests, "post") as post:
            self.c.post("/webhook/hook", json=update)
            body = post.call_args.kwargs["json"]
            self.assertIn("web_app", json.dumps(body["reply_markup"]))
            self.assertEqual(body["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"], "https://example.com")
        update["message"]["text"] = "/profile"
        with mock.patch.object(A.requests, "post") as post:
            self.c.post("/webhook/hook", json=update)
            self.assertIn("Reza", post.call_args.kwargs["json"]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
