# ERP lab deploy path (lab-03)

RT-927 implements the RT-926 design ([Confluence 60391425](https://rahmaqanater.atlassian.net/wiki/spaces/RETAIL/pages/60391425)). The path does a manual, owner-approved, recorded deploy of the ERPNext + Connector image to lab-03. The image comes from GHCR and is attested. A backup is always taken first, and no rollback can lose a sale that Backend-Core already treats as posted.

| File | Role |
| --- | --- |
| `erp.Dockerfile` | The image recipe. It uses the digest-pinned `frappe/erpnext` base and adds the Connector tree. |
| `.github/workflows/build-erp-image.yml` | Builds one main commit, pushes it to `ghcr.io/kemetra-labs/rt-erpnext`, and attests it. Manual dispatch, from main only. |
| `.github/workflows/deploy-erp-lab.yml` | Runs the preflight in GitHub only. With `dry_run=false` and `aws-lab` approval, it then runs `RT-ErpLabDeploy` over SSM. |
| `lab/ssm/RT-ErpLabDeploy.json` | The five host steps. **This document is the security boundary.** |
| `lab/rollback_class.py` | Decides between image-only and restore for a move from the current to the target Connector tree. |
| `lab/preresume_probe.py` | The checks that run before posting resumes (embedded in the document). |
| `lab/tree_hash.py` | Hashes the installed Connector tree for step 1 (embedded in the document). |
| `lab/restore.sh` | The owner-run restore. **It refuses after resume.** |
| `tests/` | Stdlib and pytest tests. They run in the `connector/unit-tests-and-lint` job. |

## The one rule

A database restore is allowed **only before the scheduler resumes**. The ledger records `resumedAt` before the workers start. After that point, posted sales are marked posted in Backend-Core and are never offered again, so a restore would delete invoices from ERPNext with no way back. From that point the only way out is **forward-fix**: deploy a corrected image.

`restore.sh` refuses in either of these cases:

- the run's ledger has `resumedAt`;
- the host-wide marker `/var/backups/rt-erp/last-resumed-at` is not older than the run's `startedAt`.

Every resume writes that marker before the workers start, including a recovery that restarts them after an early failure. So a later run that resumed, such as the `hold=false` redeploy after deploy #0, closes the restore window of every earlier run.

| When it fails | What to do |
| --- | --- |
| Step 1 (check) or step 2 before the swap | Nothing changed. The document restarts the workers. |
| After the swap, before resume | The workers stay down. If the rollback class is `image-only`, redeploy the previous image. Otherwise, or if that fails, run `restore.sh`. |
| After resume | Forward-fix only. |

## Owner steps (once, before the first live run)

Each step is a separate owner gate (design section 6). The issue itself makes no AWS call and no GitHub settings change.

1. **Build deploy #0.** Dispatch `build-erp-image.yml` from main with `source_sha` set to the full SHA of `a3b31ff7`, the code lab-03 runs now (see `tools/history/crosswalk/erpnext-connector.tsv`). Read back the `RT_IMAGE` line, the attestation and the labels.
2. **Make the package public, once.** In GHCR package settings, set `rt-erpnext` visibility to **public**, so lab-03 can pull by digest without a credential. Check: an anonymous `docker pull ghcr.io/kemetra-labs/rt-erpnext@<digest>` works.
3. **Register lab-03 in SSM** with a hybrid activation and the tags `Environment=lab` and `Host=lab-03`. The activation code stays secret and never goes into Jira or the repo. SSM Agent must be 3.3.2746.0 or newer, because parameters arrive as `SSM_*` environment variables. Check: `aws ssm describe-instance-information` shows exactly one Online `mi-` node with `Host=lab-03`.
4. **Extend the deploy identity.** Add the lab-03 node ARN and the `RT-ErpLabDeploy` document ARN to the S2b policy and its permissions boundary. Nothing else changes. If the role trust pins `job_workflow_ref`, also allow `.github/workflows/deploy-erp-lab.yml@refs/heads/main`. The live job prints the observed claims before it assumes the role.
5. **Create the document** from this file, as the owner (never from CI):

   ```bash
   aws ssm create-document --name RT-ErpLabDeploy --document-type Command --document-format JSON \
     --content file://integrations/erpnext-connector/deploy/lab/ssm/RT-ErpLabDeploy.json
   ```

   Then check that the hash `aws ssm describe-document --name RT-ErpLabDeploy` returns is the sha256 of the repository file. For a later change, use `aws ssm update-document --document-version '$LATEST'`, then set it as the default version.

## Running a deploy

1. Dispatch `deploy-erp-lab.yml` with `dry_run=true`. Pass:
   - `target_digest` and `target_sha` from the build;
   - `current_sha` (deploy #0: `a3b31ff7`);
   - `current_images`, the per-service map of what lab-03 runs now. Deploy #0 is mixed: frontend and websocket run another build.
   - `jira_key`.

   The preflight verifies the attestation and labels, prints the rollback class and its reasons, and uploads a planned ledger.
2. Dispatch again with `dry_run=false`. The `aws-lab` reviewer approves.
   - **Deploy #0:** run with `hold=true` and `save_current_image=true`. Expect `RT_OUTCOME=held`. All six services then run one digest and the workers are down.
   - **Restore rehearsal** (once, with the stack still stopped): run `restore.sh <run-id> --dry-run`, then `restore.sh <run-id>`. Expect ping 200 and the previous image running.
   - Redeploy #0 with `hold=false`. Expect `RT_OUTCOME=deployed` and a `resumedAt` in the ledger.
3. **Before the first 0.3.0 move,** confirm one of these: the lab-01 Backend-Core release is at or after the Connector `contracts/backend-core/PIN` commit, or the owner has recorded a waiver. The move `a3b31ff7` → main classes **restore**, because the Connector Settings controller changed.

The host keeps its own ledger, the backup and a root-only log under `/var/backups/rt-erp/<run-id>/` and `/var/log/rt-deploy/erp-<run-id>.log`. Only strict `RT_*` lines reach the public Actions log.

## restore.sh

```bash
sudo integrations/erpnext-connector/deploy/lab/restore.sh <run-id> --dry-run
sudo integrations/erpnext-connector/deploy/lab/restore.sh <run-id>
```

What the script checks before it changes anything:

- it refuses after resume (exit 3);
- it refuses a backup whose `SHA256SUMS` or `encryption_key` is missing (exit 4);
- it refuses a saved compose file that does not have exactly one `<volume>:/var/lib/mysql` mount and one top-level `volumes:` key (exit 5);
- step 0: every image of the saved compose file must be on the host. A missing one is loaded from the run's `current-image-*.tar` (`save_current_image=true`); if there is no tar, or it is still missing, the script exits 4 before anything stops. The tars hold only the ERP images: a missing mariadb or redis image always exits 4. Whether `docker load` of an image saved by `ghcr.io/…@sha256:` digest makes that digest inspectable again depends on the host's image store; confirm it at the next restore rehearsal.

What it does:

1. Stops the project.
2. Starts MariaDB on a **new** volume (`rt-erp_mariadb-restore-<run-id>`) through an override file. The old volume is never touched.
3. Copies the backup's `encryption_key` into the site config.
4. Runs `bench --force restore` with the public and private files.
5. Puts back the compose file saved before the swap, which holds the previous image, with its MariaDB mount moved to the new volume (declared `external: true`). Then starts all services from `compose.yml` alone, with no override.
6. Waits for ping 200.

Its host assumptions are listed in its header. The restore rehearsal is what confirms them. No manual step follows a restore: `compose.yml` already mounts the new volume. As a safety net, step 1 of `RT-ErpLabDeploy` stops with `mariadb-volume-differs-from-compose` when the running MariaDB volume is not the one `compose.yml` resolves to.

**After this change merges** (RT-927 restore safety), the document in AWS must get a new version from main (owner step 5: `update-document`, then set the default version). Until then, lab-03 runs the old document without the volume check.

## Changing the embedded helpers

`RT-ErpLabDeploy.json` carries base64 copies of `lab/preresume_probe.py` (`RT_PROBE_B64`) and `lab/tree_hash.py` (`RT_TREEHASH_B64`). `test_helpers_are_embedded_and_equal_the_repo_files` fails when either one is stale. To refresh them, replace each constant's value with the base64 of the LF file:

```bash
python -c "import base64,sys;print(base64.b64encode(open(sys.argv[1],'rb').read().replace(b'\r\n',b'\n')).decode())" integrations/erpnext-connector/deploy/lab/preresume_probe.py
```

After a refresh, update the document in AWS (owner step 5).
