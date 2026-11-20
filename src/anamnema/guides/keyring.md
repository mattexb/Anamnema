# Manual Keyring Setup

This guide covers a Debian 13 desktop user's Secret Service keyring. Package
availability and command behavior were checked against official documentation;
this is not a completed clean-VM or unattended-service acceptance test.

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
This does not prove access before login or from a system service. Enabling
background services alone is not a secure unlock method. Do not edit PAM merely
to follow this guide or remove the keyring password to make a nightly job run.

The wizard records who should run each component and whether work must continue
while logged out. Headless/unattended unlock still needs a reviewed, tested
recipe. Selecting manual or guided setup does not certify that access works.
Archive import now looks up its URI using `service=anamnema`,
`entry=archive/mongodb`, `field=uri` by default. To create that item manually:

```sh
secret-tool store --label='Anamnema archive' service anamnema entry archive/mongodb field uri
```

Enter the URI at the terminal's password prompt. This entry belongs to your own
account and project namespace, never a private/shared source-project namespace.
Account creation, automatic unlock and full keyring management are not yet
implemented. Do not put credentials in setup answers as a substitute.
