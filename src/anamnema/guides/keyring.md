# Manual Keyring Setup

This guide covers a Debian 13 desktop user's Secret Service keyring, the store
Anamnema keeps its credentials in. Package names and commands follow the official
Debian and GNOME documentation; the unlocked login keyring was exercised on a
clean Debian 13 machine. Unlocking before login is not supported.

## Prepare Your Own Account

Use the Linux account that will run the Anamnema application. Database daemons
may use their own accounts; they do not thereby gain access to this keyring.
Do not run the application as root or copy another person's keyring files.

On Debian 13, the relevant packages are [gnome-keyring](https://packages.debian.org/trixie/gnome-keyring),
[libsecret-tools](https://packages.debian.org/trixie/libsecret-tools) and the
[Seahorse password manager](https://packages.debian.org/trixie/seahorse).
If needed, install them yourself with administrator approval:

```sh
sudo apt install gnome-keyring libsecret-tools seahorse
```

Open Seahorse in your normal desktop session. Create or unlock your own password
keyring. Keep a nonempty unlock password; do not save it in a script, setup JSON,
service definition or shell command. Anamnema's wizard never requests it.

## Check Without Exposing Real Credentials

Choose a unique test identifier, replacing `YOUR_UNIQUE_CHECK_ID` below. Store a
dummy value, not an actual credential, when the terminal prompts you:

```sh
secret-tool store --label='Anamnema manual check' anamnema-setup-check YOUR_UNIQUE_CHECK_ID
secret-tool lookup anamnema-setup-check YOUR_UNIQUE_CHECK_ID >/dev/null
secret-tool clear anamnema-setup-check YOUR_UNIQUE_CHECK_ID
```

The lookup should succeed; cleanup removes only items matching those attributes.
Use unique attributes to avoid updating/removing an existing item. Do not run
lookup or search on real entries in logs, screen shares or bug reports: the tool
can print secrets. See Debian's [secret-tool manual](https://manpages.debian.org/trixie/libsecret-tools/secret-tool.1.en.html).

## Reboot And Unattended Work

Desktop login can unlock the login keyring through the user's authentication
session; [GNOME documents that mechanism](https://wiki.gnome.org/Projects/GnomeKeyring/Pam).
Access before login or from a system service is not supported. Enabling
background services alone is not a secure unlock method. Do not edit PAM merely
to follow this guide or remove the keyring password to make a nightly job run.

The wizard records who should run each component and whether work must continue
while logged out; `anamnema install` refuses `run_when_logged_out: true`.
Archive import looks up its URI using `service=anamnema`, `entry=archive/mongodb`,
`field=uri` by default. `anamnema install` creates that item itself; create it
by hand only to import into a MongoDB you set up yourself:

```sh
secret-tool store --label='Anamnema archive' service anamnema entry archive/mongodb field uri
```

Enter the URI at the terminal's password prompt. This entry belongs to your own
account and the project namespace.

### Anamnema Commands

The application can store and verify one item without printing its value:

```sh
anamnema keyring put --entry archive/mongodb --field uri --confirm-replace --apply
anamnema keyring verify --entry archive/mongodb --field uri --apply
```

Supply `--config FILE` for your complete setup and, when needed,
`--archive-config FILE` for the shared bus/timeout profile. `put` requests the
value through a hidden terminal prompt. It never takes a value as a command
argument and refuses an echo fallback. `--confirm-replace` explicitly permits
creating or replacing that exact service/entry/field item. After storing,
Anamnema looks up and compares the exact bytes before reporting success. A
failed verification says the keyring may have changed; it does not claim a
rollback. The underlying helper's output is withheld on errors.

Both commands default to dry-run: no secret is read, no helper is executed and
no item changes without `--apply`. For controlled automation, `put --stdin`
reads the exact UTF-8 value through EOF rather than prompting. Newlines and
leading/trailing spaces are part of the value, as in `secret-tool`'s non-terminal
protocol; they are not trimmed. Do not place real values in shell arguments,
history, files or logs to feed that input. The optional setup `keyring.policy`
is an absolute path to nonsecret value/attribute limits and label policy.

No credential file, SSH helper or remote credential-copy command exists. Never
put credentials in setup answers.

### Credentials the installer needs

Before `anamnema install --apply`, store one bearer token (a shared secret each
request must carry) per local service: `encoder/local`, `reranker/local`,
`memory/local` and `notes/local`, field `token`. Each is 33 to 126 characters
without spaces and without a trailing newline. The [setup guide](https://github.com/mattexb/Anamnema/blob/main/docs/SETUP.md)
shows the exact commands. The entry and field names must match the search,
extraction and service files. The installer creates the database credentials
itself: `archive/mongodb:uri`, `archive/mongodb-admin:password`,
`graph/local:username` and `graph/local:password`.

### Launch A Program With Keyring Credentials

An explicit program can receive credentials in its environment rather than its
command line:

```sh
anamnema keyring run --apply --secret LLAMA_API_KEY=notes/local:token -- /absolute/path/to/llama-server --host 127.0.0.1
```

Each `--secret` is an `ENV=ENTRY:FIELD` reference, not a value. The wrapper
validates all references before lookup, then replaces itself with the program;
there is no shell and the parent environment is unchanged. Without `--apply`,
it only previews and never launches the program. Choose trusted executables:
the launched program controls its own output and can read its credentials.
Environment transport does not isolate credentials from the same user or root.
