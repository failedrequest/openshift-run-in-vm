#!/usr/bin/env python3
# Mark Saad
# nonesuch@longcount.org

# BSD 2-Clause License
# 
# Copyright (c) 2026, Mark Saad
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
# 
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""
run-in.py — Execute commands or upload & run scripts inside KubeVirt VMs via QEMU Guest Agent.

Usage:
  run-in.py -n <namespace> -v <vm-name> -c "<command-string>"
  run-in.py -n <namespace> -v <vm-name> -l <path-to-local-script>
  run-in.py -n <namespace> -v <vm-name> --context <kube-context> -c "uptime"
"""

import argparse
import base64
import json
import os
import subprocess
import sys 
import time


def parse_args():
    parser = argparse.ArgumentParser(
        description="Execute commands or upload and run scripts inside a KubeVirt VM via QEMU Guest Agent."
    )   
    parser.add_argument("-n", "--namespace", required=True, help="Kubernetes/OpenShift namespace of the VM")
    parser.add_argument("-v", "--vm", "--vm-name", dest="vm_name", required=True, help="Target VirtualMachine name")
    parser.add_argument("-c", "--command", help="String of commands to execute inside the VM")
    parser.add_argument("-l", "--local-script", help="Path to a local script file to upload and execute on the VM")
    parser.add_argument("--context", help="Kubeconfig context to use (optional)")
    parser.add_argument("--kubeconfig", help="Path to kubeconfig file (optional)")
    parser.add_argument("--timeout", type=int, default=120, help="Command execution timeout in seconds (default: 120)")
    parser.add_argument("--shell", choices=["auto", "bash", "sh", "powershell", "cmd"], default="auto",
                        help="Shell interpreter to use (default: auto)")
    parser.add_argument("--keep-uploaded-script", action="store_true",
                        help="Do not delete uploaded local script after execution")

    args = parser.parse_args()
    if not args.command and not args.local_script:
        parser.error("Either -c/--command or -l/--local-script must be specified.")
    if args.command and args.local_script:
        parser.error("Specify either -c/--command or -l/--local-script, not both.")
    return args


class QemuGuestRunner:
    def __init__(self, namespace, vm_name, context=None, kubeconfig=None):
        self.namespace = namespace
        self.vm_name = vm_name
        self.context = context
        self.kubeconfig = kubeconfig
        self.domain = f"{namespace}_{vm_name}"
        self.pod = self._get_launcher_pod()
        self.os_type = self._detect_os()

    def _build_kubectl_base(self):
        cmd = ["kubectl"]
        if self.context:
            cmd.extend(["--context", self.context])
        if self.kubeconfig:
            cmd.extend(["--kubeconfig", self.kubeconfig])
        return cmd 

    def _get_launcher_pod(self):
        cmd = self._build_kubectl_base() + [ 
            "get", "pods", "-n", self.namespace,
            "-l", f"kubevirt.io/domain={self.vm_name}",
            "-o", "jsonpath={.items[0].metadata.name}"
        ]   
        res = subprocess.run(cmd, capture_output=True, text=True)
        pod = res.stdout.strip()
        if not pod or res.returncode != 0:
            sys.stderr.write(f"Error: Unable to find virt-launcher pod for VM '{self.vm_name}' in namespace '{self.namespace}'.\n")
            if res.stderr:
                sys.stderr.write(f"{res.stderr}\n")
            sys.exit(1)
        return pod 

    def run_agent_command(self, payload):
        cmd = self._build_kubectl_base() + [ 
            "exec", "-n", self.namespace, self.pod,
            "-c", "compute", "--", "virsh", "qemu-agent-command", self.domain, json.dumps(payload)
        ]   
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"virsh qemu-agent-command failed: {res.stderr.strip()}")
        try:
            return json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Failed to parse qemu-agent response: {res.stdout.strip()}") from exc 

    def _detect_os(self):
        try:
            resp = self.run_agent_command({"execute": "guest-get-osinfo"})
            os_id = resp.get("return", {}).get("id", "").lower()
            os_name = resp.get("return", {}).get("name", "").lower()
            if "win" in os_id or "win" in os_name or "mswindows" in os_id:
                return "windows"
        except Exception:
            pass
        return "linux"

    def upload_file(self, local_path, remote_path):
        with open(local_path, "rb") as f:
            content = f.read()

        open_payload = { 
            "execute": "guest-file-open",
            "arguments": {
                "path": remote_path,
                "mode": "w+b" if self.os_type == "windows" else "w+"
            }   
        }   
        res = self.run_agent_command(open_payload)
        handle = res.get("return")
        if handle is None:
            raise RuntimeError(f"Failed to open remote file '{remote_path}' for writing.")

        try:
            chunk_size = 48 * 1024
            for i in range(0, len(content), chunk_size):
                chunk = content[i:i + chunk_size]
                b64_buf = base64.b64encode(chunk).decode("ascii")
                write_payload = { 
                    "execute": "guest-file-write",
                    "arguments": {
                        "handle": handle,
                        "buf-b64": b64_buf
                    }   
                }   
                self.run_agent_command(write_payload)
            self.run_agent_command({"execute": "guest-file-flush", "arguments": {"handle": handle}})
        finally:
            self.run_agent_command({"execute": "guest-file-close", "arguments": {"handle": handle}})

    def execute_and_stream(self, exec_path, exec_args, timeout=120):
        exec_payload = { 
            "execute": "guest-exec",
            "arguments": {
                "path": exec_path,
                "arg": exec_args,
                "capture-output": True
            }   
        }   
        resp = self.run_agent_command(exec_payload)
        pid = resp.get("return", {}).get("pid")
        if not pid:
            raise RuntimeError(f"guest-exec failed to return a PID: {resp}")

        status_payload = { 
            "execute": "guest-exec-status",
            "arguments": {"pid": pid}
        }   

        start_time = time.time()
        while True:
            status_resp = self.run_agent_command(status_payload)
            ret = status_resp.get("return", {}) 
            if ret.get("exited", False):
                out_b64 = ret.get("out-data", "") 
                err_b64 = ret.get("err-data", "") 
                out_str = base64.b64decode(out_b64).decode("utf-8", errors="replace") if out_b64 else ""
                err_str = base64.b64decode(err_b64).decode("utf-8", errors="replace") if err_b64 else ""
                exitcode = ret.get("exitcode", 0)
                return exitcode, out_str, err_str

            if time.time() - start_time > timeout:
                return -1, "", f"Command timed out after {timeout} seconds (guest PID {pid})"
            time.sleep(0.5)


def main():
    args = parse_args()
    runner = QemuGuestRunner(
        namespace=args.namespace,
        vm_name=args.vm_name,
        context=args.context,
        kubeconfig=args.kubeconfig
    )   

    is_windows = runner.os_type == "windows"

    if args.local_script:
        if not os.path.isfile(args.local_script):
            sys.stderr.write(f"Error: Local script '{args.local_script}' not found.\n")
            sys.exit(1)

        base_filename = os.path.basename(args.local_script)
        timestamp = int(time.time())

        if is_windows:
            remote_path = f"C:\\Windows\\Temp\\guest_{timestamp}_{base_filename}"
            runner.upload_file(args.local_script, remote_path)
            if args.shell == "cmd" or base_filename.lower().endswith((".bat", ".cmd")):
                exec_path = "cmd.exe"
                exec_args = ["/c", remote_path]
            else:
                exec_path = "powershell.exe"
                exec_args = ["-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", remote_path]
        else:
            remote_path = f"/tmp/guest_{timestamp}_{base_filename}"
            runner.upload_file(args.local_script, remote_path)
            runner.execute_and_stream("/bin/chmod", ["+x", remote_path], timeout=10)
            if args.shell == "sh":
                exec_path = "/bin/sh"
                exec_args = [remote_path]
            else:
                exec_path = "/bin/bash"
                exec_args = [remote_path]

        try:
            exitcode, out, err = runner.execute_and_stream(exec_path, exec_args, timeout=args.timeout)
            if out:
                sys.stdout.write(out)
                sys.stdout.flush()
            if err:
                sys.stderr.write(err)
                sys.stderr.flush()
            sys.exit(exitcode)
        finally:
            if not args.keep_uploaded_script:
                if is_windows:
                    runner.execute_and_stream("cmd.exe", ["/c", f"del /f /q \"{remote_path}\""], timeout=10)
                else:
                    runner.execute_and_stream("/bin/rm", ["-f", remote_path], timeout=10)

    elif args.command:
        if is_windows:
            if args.shell == "cmd":
                exec_path = "cmd.exe"
                exec_args = ["/c", args.command]
            else:
                exec_path = "powershell.exe"
                exec_args = ["-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", args.command]
        else:
            if args.shell == "sh":
                exec_path = "/bin/sh"
                exec_args = ["-c", args.command]
            else:
                exec_path = "/bin/bash"
                exec_args = ["-c", args.command]

        exitcode, out, err = runner.execute_and_stream(exec_path, exec_args, timeout=args.timeout)
        if out:
            sys.stdout.write(out)
            sys.stdout.flush()
        if err:
            sys.stderr.write(err)
            sys.stderr.flush()
        sys.exit(exitcode)


if __name__ == "__main__":
    main()
