# openshift-run-in-vm

A tool to run commands or scripts inside KubeVirt/OpenShift HCO-hosted VMs without SSH, using the QEMU Guest Agent as the transport layer.

## What it does

`run-in.py` communicates with a running VM through its `virt-launcher` pod via `virsh qemu-agent-command`. It can:

- Execute an arbitrary shell command inside the VM (`-c`)
- Upload a local script, run it, then clean it up automatically (`-l`)
- Auto-detect whether the guest is Linux or Windows and choose the right interpreter
- Work with any kubeconfig context or custom kubeconfig file

No SSH, no exposed ports, no in-guest credentials required — only `kubectl` access to the namespace.

## Requirements

- Python 3.6+
- `kubectl` on your `PATH`, authenticated to the cluster
- The target VM must be running with `qemu-guest-agent` installed and active
- The `virt-launcher` pod must be reachable via `kubectl exec`

## Usage

```
run-in.py -n <namespace> -v <vm-name> -c "<command>"
run-in.py -n <namespace> -v <vm-name> -l <path-to-local-script>
```

### Flags

| Flag | Description |
|---|---|
| `-n`, `--namespace` | Kubernetes/OpenShift namespace the VM lives in (**required**) |
| `-v`, `--vm` | VirtualMachine name (**required**) |
| `-c`, `--command` | Command string to run inside the VM |
| `-l`, `--local-script` | Path to a local script to upload and execute |
| `--context` | Kubeconfig context to use (optional) |
| `--kubeconfig` | Path to a kubeconfig file (optional) |
| `--timeout` | Execution timeout in seconds (default: `120`) |
| `--shell` | Shell to use: `auto`, `bash`, `sh`, `powershell`, `cmd` (default: `auto`) |
| `--keep-uploaded-script` | Skip deleting the uploaded script after execution |

`-c` and `-l` are mutually exclusive.

## Examples

Run a one-liner on a Linux VM:
```sh
python3 run-in.py -n my-namespace -v my-vm -c "hostname && uptime"
```

Upload and run a local shell script:
```sh
python3 run-in.py -n my-namespace -v my-vm -l ./setup.sh
```

Run a PowerShell command on a Windows VM:
```sh
python3 run-in.py -n my-namespace -v my-vm -c "Get-ComputerInfo" --shell powershell
```

Use a specific kubeconfig context:
```sh
python3 run-in.py -n my-namespace -v my-vm --context prod-cluster -c "df -h"
```

## How it works

1. Looks up the `virt-launcher` pod for the target VM using the `kubevirt.io/domain` label.
2. Calls `virsh qemu-agent-command` inside the `compute` container of that pod.
3. For `-c`: sends a `guest-exec` payload directly with the command string.
4. For `-l`: uses `guest-file-open` / `guest-file-write` / `guest-file-close` to upload the script in 48 KB base64-encoded chunks, marks it executable, then runs it via `guest-exec`.
5. Polls `guest-exec-status` until the process exits, then prints stdout/stderr and exits with the same exit code.

## License

BSD 2-Clause — see source header in [`run-in.py`](run-in.py).
