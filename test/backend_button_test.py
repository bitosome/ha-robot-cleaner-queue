"""Shared-plan button discovery and permission-context routing; no robot calls."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

SOURCE = Path(__file__).resolve().parents[1] / 'custom_components/robot_cleaner_queue/button.py'
class Entity:
    def async_on_remove(self, fn): self.remove = fn
    def async_write_ha_state(self): pass


def module():
    tree = ast.parse(SOURCE.read_text())
    tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    env = {'ButtonEntity': Entity, 'DOMAIN': 'robot_cleaner_queue', 'callback': lambda f: f}
    exec(compile(tree, str(SOURCE), 'exec'), env)
    return env

class ButtonTests(unittest.IsolatedAsyncioTestCase):
    def manager(self):
        listeners=[]
        return NS(saved_presets={'vacuum.test': {}}, subscribe=lambda f:listeners.append(f),
                  hass=NS(states=NS(get=lambda _:NS(attributes={'friendly_name':'Robot'}))), listeners=listeners)

    async def test_restored_and_new_plans_discovered_once(self):
        m=self.manager(); hass=NS(data={'robot_cleaner_queue':m});added=[]
        await module()['async_setup_platform'](hass,{},added.extend)
        self.assertEqual([e.vacuum for e in added],['vacuum.test'])
        m.listeners[0]();self.assertEqual(len(added),1)
        m.saved_presets['vacuum.second']={};m.listeners[0]()
        self.assertEqual([e.vacuum for e in added],['vacuum.test','vacuum.second'])

    async def test_press_preserves_context_and_uses_single_controller(self):
        e=module()['SharedPlanButton'](self.manager(),'vacuum.test')
        call=AsyncMock();e.hass=NS(services=NS(async_call=call));e._context=object()
        await e.async_press()
        call.assert_awaited_once_with('robot_cleaner_queue','control',{'command':'toggle_saved','vacuum':'vacuum.test'},blocking=True,context=e._context)

    async def test_rejection_propagates_without_fallback(self):
        e=module()['SharedPlanButton'](self.manager(),'vacuum.test');e._context=None
        call=AsyncMock(side_effect=ValueError('No rooms enabled'));e.hass=NS(services=NS(async_call=call))
        with self.assertRaisesRegex(ValueError,'No rooms'):await e.async_press()
        self.assertEqual(call.await_count,1)

if __name__=='__main__':unittest.main()
