"""Preserve Home Assistant entity-control authorization across deferred commands."""


async def async_require_control(auth, user_id, entity_ids, policy):
    """Re-fetch the caller on every dispatch; never cache a permission decision."""
    if user_id is None:
        return  # Home Assistant automations retain their normal system context.
    user = await auth.async_get_user(user_id)
    if user is None or not user.is_active:
        raise PermissionError("The initiating Home Assistant user is missing or inactive.")
    if user.is_admin:
        return
    for entity_id in set(entity_ids):
        if entity_id and not user.permissions.check_entity(entity_id, policy):
            raise PermissionError("The user cannot control every entity in this cleaning sequence.")
