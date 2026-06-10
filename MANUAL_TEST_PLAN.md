# Manual Test Plan

This plan covers the user, group, permissions, and account-management rework.

## 1. Startup

1. Start the app:

```powershell
python webui.py
```

2. Log in as an admin.
3. Open `/admin/permissions`.

Expected:

- The Permissions page loads.
- Users and Groups tabs both work.
- Existing users and groups are still present.
- The Admin group cannot be deleted.

## 2. Create Groups

1. Open the Groups tab.
2. Create `Schedule Test`.
3. Give it Home and Schedule permissions.
4. Create `Timers Test`.
5. Give it Home and Timers permissions.
6. Create `VideoHub Preset 3`.
7. Give it VideoHub permission and allowed preset ID `3`.

Expected:

- New groups appear in the group list.
- Group permission changes auto-save.
- A single allowed preset value such as `3` stays saved and does not turn blank.

## 3. Create Users

1. Open the Users tab.
2. Try to create a user without full name, email, or password.
3. Create a user with username, full name, unique email, password, and `Schedule Test`.
4. Try to create another user with the same username.
5. Try to create another user with the same email.
6. Try a password shorter than the configured minimum.

Expected:

- Required-field prompts appear in the browser style.
- Duplicate username and duplicate email are blocked.
- Short passwords are blocked.
- The form keeps entered values when validation fails.
- A valid user is created successfully.

## 4. Open User Detail Page

1. Search for a user in the Users tab.
2. Click the user row.

Expected:

- The app opens `/admin/users/<id>`.
- The page shows Profile, Access, Security, Sessions, Effective Permissions, and Activity.

## 5. Edit Profile

1. Change the user's username.
2. Change full name.
3. Change email.
4. Save the profile.
5. Try using an email already assigned to another user.

Expected:

- Valid profile changes save.
- Duplicate email is blocked.
- Existing old users with blank email/full name can still exist, but editing them requires filling those fields.

## 6. Groups And Effective Permissions

1. On the user detail page, add `Timers Test`.
2. Confirm the access change auto-saves.
3. Check Effective Permissions.
4. Remove `Timers Test`.
5. Check Effective Permissions again.

Expected:

- The user inherits permissions from all assigned groups.
- Each allowed permission shows which group grants it.
- Removing a group removes only that group's permissions.

## 7. Login Permission Check

1. Log out.
2. Log in as a user assigned only to `Schedule Test`.
3. Try Schedule and Timers.
4. Log in as a user assigned to both `Schedule Test` and `Timers Test`.

Expected:

- Schedule-only user can access Schedule but not Timers.
- Multi-group user can access both.

## 8. Password Reset

1. Open a user detail page.
2. Reset the password with a typed password.
3. Log in as that user with the new password.
4. Generate a temporary password.
5. Use the shown temporary password to log in.

Expected:

- Typed reset works.
- Generated password is shown once.
- Old password no longer works.
- Password changed date updates.

## 9. Force Password Change

1. Reset a user's password.
2. Leave Force password change on next login checked.
3. Log in as that user.

Expected:

- The user is sent to Change Password.
- They cannot continue until they set a new password.
- After changing it, normal navigation works again.

## 10. Lockout

1. Set Failed Login Lockout Attempts on the Config page.
2. Log out.
3. Enter the wrong password for a test user until the threshold is reached.
4. Try the correct password.
5. Log in as admin and open the user's detail page.
6. Unlock the account.
7. Log in as the test user again.

Expected:

- Failed login count increases.
- The account locks at the configured threshold.
- Locked users cannot log in.
- Admin unlock clears the failed count and restores access.

## 11. Account Status

1. Open a user detail page.
2. Turn Account active off.
3. Try to log in as that user.
4. Turn Account active back on.

Expected:

- Inactive users cannot log in.
- Reactivated users can log in again.

## 12. Sessions

1. Log in as a test user in another browser or private window.
2. Open that user's detail page as admin.
3. Check Sessions.
4. Click Sign out everywhere.
5. Refresh the test user's browser.

Expected:

- Active session appears.
- Sign out everywhere revokes the session.
- The test user is sent back to login.

## 13. Delete Safety

1. Try to delete your own admin account.
2. Try to remove admin access from the last active admin user.
3. Try to lock or delete the last active admin user.
4. Delete a normal test user.

Expected:

- Self-delete is blocked.
- The last active admin-capable user is protected.
- Normal user deletion asks for confirmation and then succeeds.

## 14. Audit

1. Create a user.
2. Edit profile fields.
3. Change groups.
4. Reset password.
5. Lock and unlock the account.
6. Sign out everywhere.
7. Check the Activity section.

Expected:

- Recent activity shows the admin/user-management actions.
- Login success, login failure, lockout, password reset, unlock, and session revocation are visible where relevant.

## 15. UI Checks

1. Check Users tab search.
2. Check Groups tab search/selection.
3. Check group search on Create User and User Detail.
4. Resize the browser.

Expected:

- Lists stay scrollable.
- Cards do not overlap.
- The user detail page remains readable on smaller screens.
