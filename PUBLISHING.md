# Publishing the first release

1. Create a PyPI account, verify your email address, and enable two-factor
   authentication: https://pypi.org/account/register/
2. Open your account's Publishing page:
   https://pypi.org/manage/account/publishing/
3. Add a pending GitHub Trusted Publisher using exactly these values:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `ada-rs232` |
   | GitHub owner | `RebbePod` |
   | GitHub repository | `ada-rs232-python` |
   | Workflow filename | `publish.yml` |
   | Environment name | `pypi` |

4. Review the source, tests, and release artifacts. Once the publisher is added,
   create a GitHub release for tag `v0.1.0` at the tested commit. The publish
   workflow builds both the wheel and source distribution from that tag and
   uploads them using GitHub's short-lived identity.
5. Verify both files appear at https://pypi.org/project/ada-rs232/0.1.0/ before
   submitting the Home Assistant Core PR.

No PyPI API token or GitHub secret is needed. A pending publisher does not reserve
the package name until the first successful publish. If publishing fails, fix the
publisher settings and rerun the workflow from the tagged release.

Reference: https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/
