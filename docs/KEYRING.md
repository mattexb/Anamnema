# Manual Keyring Setup

The [complete guide](../src/anamnema/guides/keyring.md) is packaged with Anamnema
so it is also available outside the source checkout:

```sh
anamnema setup --keyring-guide
```

It covers the Debian 13 desktop path, the access tokens the installer needs and
why unlocking before login is not supported. The wizard records manual or guided
choices; it does not configure or unlock a keyring. Never put passwords in setup
answers or shell arguments.
