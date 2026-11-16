from __future__ import annotations

import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from reolink_aio.baichuan.baichuan import Baichuan
from reolink_aio.exceptions import InvalidParameterError

FIXTURES = Path(__file__).parent / "fixtures"
HEADER_LEN = 24  # message class 1464

# cmd 604 (sceneCfg) responses of a Home Hub (firmware v3.3.0.601) for its scenes disarm, home and away,
# trimmed from <ipc> chn 0-31 and <iot> chn 0-63 to <ipc> chn 0-8 and <iot> chn 0-3
SCENE_CFG = {scene_id: (FIXTURES / f"home_hub_scene_{scene_id}.xml").read_text(encoding="utf-8").rstrip("\n") for scene_id in (1, 2, 3)}


def with_switch(xml: str, kind: str, channel: int, mask: int) -> str:
    """Change one <switch> in a raw sceneCfg with a plain text substitution"""
    new, count = re.subn(rf"(<{kind}>\n<chn>{channel}</chn>\n<switch>)\d+(</switch>)", rf"\g<1>{mask}\g<2>", xml)
    assert count == 1
    return new


def scene_id_of(body: str) -> int:
    match = re.search(r"<id>(\d+)</id>", body)
    assert match is not None
    return int(match.group(1))


class _FakeHubConnection:
    """Replaces the Baichuan connection, answers cmd 604 with the stored sceneCfg and stores the sceneCfg of cmd 605"""

    def __init__(self) -> None:
        self.scene_cfg = dict(SCENE_CFG)
        self.written: list[tuple[int, str]] = []

    async def send(self, data: bytes, cmd_id: int, *_args) -> tuple[bytes, int, bytes]:
        body = data[HEADER_LEN:].decode("utf-8")  # no extension without channel, encryption is mocked out
        self.written.append((cmd_id, body))
        response = ""
        if cmd_id == 604:
            response = self.scene_cfg[scene_id_of(body)]
        elif cmd_id == 605:
            self.scene_cfg[scene_id_of(body)] = body
        return data[:HEADER_LEN] + response.encode("utf-8"), HEADER_LEN, b""

    def bodies(self, cmd_id: int) -> list[str]:
        return [body for written_cmd_id, body in self.written if written_cmd_id == cmd_id]


class TestBaichuanSceneTasks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.baichuan = Baichuan(
            host="127.0.0.1",
            username="user",
            password="password",
            http_api=SimpleNamespace(nvr_name="test", _updating=False),
        )
        self.hub = _FakeHubConnection()
        self.baichuan._logged_in = True
        self.baichuan._connection = self.hub  # type: ignore[assignment]
        self.baichuan._connect_if_needed = AsyncMock()  # type: ignore[method-assign]
        self.baichuan._aes_encrypt = Mock(side_effect=lambda body: body)  # type: ignore[method-assign]
        self.baichuan._decrypt = Mock(side_effect=lambda data, len_header, *_args: data[len_header:].decode("utf-8"))  # type: ignore[method-assign]

    async def _load_scenes(self) -> None:
        for scene_id in (1, 2, 3):
            await self.baichuan.get_scene_info(scene_id)
        self.hub.written.clear()

    def _sent_605(self) -> str:
        bodies = self.hub.bodies(605)
        self.assertEqual(len(bodies), 1)
        return bodies[0]

    async def test_get_scene_info_parses_names_and_tasks(self) -> None:
        await self._load_scenes()

        self.assertEqual(self.baichuan.scene_names, ["disarm", "home", "away"])
        for channel in range(9):
            self.assertEqual(self.baichuan.scene_tasks(channel, scene_name="disarm"), [])
        self.assertEqual(self.baichuan.scene_tasks(0, scene_id=2), ["record", "push"])  # 9
        for channel in range(1, 9):
            self.assertEqual(self.baichuan.scene_tasks(channel, scene_name="home"), ["record"])  # 1
        for channel in range(3):
            self.assertEqual(self.baichuan.scene_tasks(channel, scene_name="away"), ["record", "push", "linkage", "track"])  # 169
        for channel in range(3, 8):
            self.assertEqual(self.baichuan.scene_tasks(channel, scene_name="away"), ["record", "ftp", "email", "push", "linkage", "track"])  # 175
        self.assertEqual(self.baichuan.scene_tasks(8, scene_name="away"), ["record", "ftp", "email", "push", "track", "light"])  # 399

        for channel in range(4):
            self.assertEqual(self.baichuan.scene_tasks(channel, scene_name="disarm", iot=True), [])
            self.assertEqual(self.baichuan.scene_tasks(channel, scene_name="home", iot=True), [])
            self.assertEqual(self.baichuan.scene_tasks(channel, scene_name="away", iot=True), ["linkage", "device"])  # 3

    async def test_scene_tasks_unknown_scene_or_channel(self) -> None:
        await self._load_scenes()

        self.assertEqual(self.baichuan.scene_tasks(0, scene_name="unknown"), [])
        self.assertEqual(self.baichuan.scene_tasks(0, scene_id=4), [])
        self.assertEqual(self.baichuan.scene_tasks(9, scene_name="away"), [])

    async def test_set_scene_tasks_only_changes_the_target_switch(self) -> None:
        await self._load_scenes()

        await self.baichuan.set_scene_tasks(3, [], scene_name="home")

        # byte-identical to the received sceneCfg except <ipc> chn 3: 1 -> 0
        self.assertEqual(self._sent_605(), with_switch(SCENE_CFG[2], "ipc", 3, 0))
        self.assertEqual(self.baichuan.scene_tasks(3, scene_name="home"), [])
        self.assertEqual(self.baichuan.scene_tasks(2, scene_name="home"), ["record"])

    async def test_set_scene_tasks_by_id_with_task_names(self) -> None:
        await self._load_scenes()

        await self.baichuan.set_scene_tasks(1, ["push", "record", "light"], scene_id=1)

        self.assertEqual(self._sent_605(), with_switch(SCENE_CFG[1], "ipc", 1, 0b100001001))
        self.assertEqual(self.baichuan.scene_tasks(1, scene_id=1), ["record", "push", "light"])

    async def test_set_scene_tasks_iot(self) -> None:
        await self._load_scenes()

        await self.baichuan.set_scene_tasks(0, ["device"], scene_name="away", iot=True)

        self.assertEqual(self._sent_605(), with_switch(SCENE_CFG[3], "iot", 0, 2))
        self.assertEqual(self.baichuan.scene_tasks(0, scene_name="away", iot=True), ["device"])
        self.assertEqual(self.baichuan.scene_tasks(0, scene_name="away"), ["record", "push", "linkage", "track"])

    async def test_set_scene_tasks_round_trip_is_unchanged(self) -> None:
        await self._load_scenes()

        for scene_id in (1, 2, 3):
            self.hub.written.clear()
            await self.baichuan.set_scene_tasks(8, self.baichuan.scene_tasks(8, scene_id=scene_id), scene_id=scene_id)
            self.assertEqual(self._sent_605(), SCENE_CFG[scene_id])

    async def test_set_scene_tasks_keeps_unknown_bits(self) -> None:
        self.hub.scene_cfg[2] = with_switch(SCENE_CFG[2], "ipc", 0, (1 << 12) | 9)
        await self._load_scenes()

        await self.baichuan.set_scene_tasks(0, ["email"], scene_name="home")

        self.assertEqual(self._sent_605(), with_switch(SCENE_CFG[2], "ipc", 0, (1 << 12) | 4))

    async def test_set_scene_tasks_invalid_input(self) -> None:
        await self._load_scenes()
        self.baichuan._scenes[-1] = "off"

        with self.assertRaises(InvalidParameterError):
            await self.baichuan.set_scene_tasks(0, ["record"], scene_name="unknown")
        with self.assertRaises(InvalidParameterError):
            await self.baichuan.set_scene_tasks(0, ["record"], scene_id=-1)
        with self.assertRaises(InvalidParameterError):
            await self.baichuan.set_scene_tasks(0, ["siren"], scene_id=2)
        with self.assertRaises(InvalidParameterError):
            await self.baichuan.set_scene_tasks(0, ["record"], scene_id=2, iot=True)  # camera task for an IoT device
        with self.assertRaises(InvalidParameterError):
            await self.baichuan.set_scene_tasks(9, ["record"], scene_id=2)
        self.assertEqual(self.hub.bodies(605), [])
