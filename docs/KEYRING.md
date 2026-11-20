# Manual Keyring Setup

The [complete guide](../src/anamnema/guides/keyring.md) is packaged with Anamnema
so it is also available outside the source checkout:

```sh
anamnema setup --keyring-guide
```

It covers the Debian 13 desktop path and the limits of unattended access.
The wizard records manual/guided choices; it does not yet configure or unlock
a keyring. Never put passwords in setup answers or shell arguments.
