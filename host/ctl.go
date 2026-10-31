package host

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"golang.org/x/term"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

func readProtectedLine(input *os.File) (string, error) {
	if term.IsTerminal(int(input.Fd())) {
		b, e := term.ReadPassword(int(input.Fd()))
		if e != nil {
			return "", e
		}
		return strings.TrimSpace(string(b)), nil
	}
	b, e := bufio.NewReader(io.LimitReader(input, 16384)).ReadString('\n')
	if e != nil && e != io.EOF {
		return "", e
	}
	return strings.TrimSpace(b), nil
}
func adminClient(c Config) *HelperClient {
	return &HelperClient{HTTP: unixPeerClient(c.AdminSocket, 16*time.Minute, &c.GatewayUID)}
}
func ctlAdmin(ctx context.Context, c Config, path string, in, out any) error {
	return adminClient(c).call(ctx, path, in, out)
}
func fixedService(action, unit string) error {
	if os.Getuid() != 0 {
		return errors.New("service maintenance requires local root")
	}
	if action != "start" && action != "stop" && action != "restart" {
		return errors.New("unknown service action")
	}
	if unit != "personal-bot-gateway.service" && unit != "personal-bot-helper.service" {
		return errors.New("unknown project service")
	}
	cmd := exec.Command("/usr/bin/systemctl", action, unit)
	cmd.Stdin = nil
	return cmd.Run()
}
func setHelperImage(config, image string) error {
	if !immutableImagePattern.MatchString(image) {
		return errors.New("image must be pinned by digest")
	}
	c, e := LoadHelperConfig(config)
	if e != nil {
		return e
	}
	c.Image = image
	b, e := json.MarshalIndent(c, "", "  ")
	if e != nil {
		return e
	}
	dir := filepath.Dir(config)
	f, e := os.CreateTemp(dir, ".helper-config-")
	if e != nil {
		return e
	}
	name := f.Name()
	defer os.Remove(name)
	if e = f.Chmod(0600); e != nil {
		f.Close()
		return e
	}
	if _, e = f.Write(b); e != nil {
		f.Close()
		return e
	}
	if e = f.Sync(); e != nil {
		f.Close()
		return e
	}
	if e = f.Close(); e != nil {
		return e
	}
	if e = os.Rename(name, config); e != nil {
		return e
	}
	return syncDirectory(dir)
}

// RunCTL handles only fixed administrative operations; passwords/age identities
// are read from protected stdin, never accepted as command-line arguments.
func RunCTL(args []string, in *os.File, out, errOut io.Writer) error {
	global := flag.NewFlagSet("personal-botctl", flag.ContinueOnError)
	global.SetOutput(errOut)
	configPath := global.String("config", "/etc/personal-bot/host.json", "gateway config")
	helperPath := global.String("helper-config", "/etc/personal-bot/helper.json", "root-owned helper config")
	if e := global.Parse(args); e != nil {
		return e
	}
	rest := global.Args()
	if len(rest) == 0 {
		return errors.New("commands: init, doctor, status, backup, suspend, resume, retry, stop, recover-owner, restore, upgrade, rollback")
	}
	c, e := LoadConfig(*configPath)
	if e != nil {
		return e
	}
	ctx, cancel := context.WithTimeout(context.Background(), 16*time.Minute)
	defer cancel()
	var result any
	switch rest[0] {
	case "init":
		s, e := OpenStore(c.StateDir)
		if e != nil {
			return e
		}
		defer s.Close()
		if _, e = s.SetupToken(); e != nil {
			return e
		}
		result = map[string]string{"setup_token_file": filepath.Join(c.StateDir, "setup.token"), "gateway_public_key": filepath.Join(c.StateDir, "assertion.pub")}
	case "doctor", "status", "resume", "retry":
		var v map[string]any
		if e = ctlAdmin(ctx, c, "/v1/"+rest[0], struct{}{}, &v); e != nil {
			return e
		}
		result = v
	case "suspend":
		reason := "local administrative suspension"
		if len(rest) > 1 {
			reason = strings.Join(rest[1:], " ")
		}
		var v map[string]any
		if e = ctlAdmin(ctx, c, "/v1/suspend", map[string]string{"reason": reason}, &v); e != nil {
			return e
		}
		result = v
	case "stop":
		var v map[string]any
		if e = ctlAdmin(ctx, c, "/v1/stop", map[string]bool{"force": false}, &v); e != nil {
			return e
		}
		result = v
	case "backup":
		var v BackupInfo
		if e = ctlAdmin(ctx, c, "/v1/backup", struct{}{}, &v); e != nil {
			return e
		}
		result = v
	case "recover-owner":
		if os.Getuid() != 0 {
			return errors.New("owner recovery requires local root")
		}
		fmt.Fprintln(errOut, "输入新密码（至少12字节；不写入命令行）：")
		password, e := readProtectedLine(in)
		if e != nil {
			return e
		}
		var v map[string]any
		if e = ctlAdmin(ctx, c, "/v1/recover-owner", map[string]string{"new_password": password}, &v); e != nil {
			return e
		}
		result = v
	case "restore", "rollback":
		flags := flag.NewFlagSet(rest[0], flag.ContinueOnError)
		flags.SetOutput(errOut)
		id := flags.String("backup", "", "completed backup ID, never a filesystem path")
		confirm := flags.Bool("confirm", false, "apply verified restore; unfinished tasks require review")
		if e = flags.Parse(rest[1:]); e != nil {
			return e
		}
		if !validBackupID(*id) {
			return errors.New("provide --backup with a completed archive ID")
		}
		if os.Getuid() != 0 {
			return errors.New("restore requires local root")
		}
		fmt.Fprintln(errOut, "输入 age 恢复私钥（仅在本次进程内使用，不保存）：")
		identity, e := readProtectedLine(in)
		if e != nil {
			return e
		}
		var current BackupInfo
		if e = ctlAdmin(ctx, c, "/v1/backup", struct{}{}, &current); e != nil {
			return fmt.Errorf("pre-restore backup failed: %w", e)
		}
		helper := NewHelperClient(c.HelperSocket)
		helper.HTTP.Timeout = 15 * time.Minute
		request := RestoreRequest{ID: *id, Identity: identity, PreviousBackupID: current.ID, Confirm: *confirm}
		var preview ArchiveManifest
		if e = helper.call(ctx, "/v1/restore-preview", request, &preview); e != nil {
			return e
		}
		if !*confirm {
			result = map[string]any{"preview": preview, "current_state_backup": current, "notice": "再次使用 --confirm 应用。旧任务需核对、排程暂停；记忆和删除状态会回到备份时间。"}
			break
		}
		if e = fixedService("stop", "personal-bot-gateway.service"); e != nil {
			return e
		}
		var restored struct {
			Restored  bool   `json:"restored"`
			Image     string `json:"required_image"`
			Epoch     string `json:"deployment_epoch"`
			Suspended bool   `json:"safety_suspended"`
			Notice    string `json:"notice"`
		}
		if e = helper.call(ctx, "/v1/restore", request, &restored); e != nil {
			_ = fixedService("start", "personal-bot-gateway.service")
			return e
		}
		if e = setHelperImage(*helperPath, restored.Image); e != nil {
			return fmt.Errorf("data restored but image configuration failed; leave service stopped: %w", e)
		}
		if e = fixedService("restart", "personal-bot-helper.service"); e != nil {
			return e
		}
		if e = fixedService("start", "personal-bot-gateway.service"); e != nil {
			return e
		}
		result = map[string]any{"result": restored, "current_state_backup": current, "next_step": "Review the restore notice, then explicitly run personal-botctl resume to allow the review-only restored runtime."}
	case "upgrade":
		flags := flag.NewFlagSet("upgrade", flag.ContinueOnError)
		flags.SetOutput(errOut)
		image := flags.String("image", "", "already locally present native image@sha256:digest")
		if e = flags.Parse(rest[1:]); e != nil {
			return e
		}
		if os.Getuid() != 0 {
			return errors.New("upgrade requires local root")
		}
		if !immutableImagePattern.MatchString(*image) {
			return errors.New("provide --image pinned by digest")
		}
		helperConfig, e := LoadHelperConfig(*helperPath)
		if e != nil {
			return e
		}
		docker := NewDockerManager(helperConfig)
		var imageInfo struct {
			Architecture string
			OS           string
		}
		if _, e = docker.docker(ctx, "GET", "/images/"+*image+"/json", nil, &imageInfo); e != nil {
			return errors.New("new image must already be present; prepare it outside shared-host workload tests")
		}
		if imageInfo.OS != "linux" || imageInfo.Architecture != runtime.GOARCH {
			return errors.New("upgrade image must match this native Linux architecture")
		}
		var current BackupInfo
		if e = ctlAdmin(ctx, c, "/v1/backup", struct{}{}, &current); e != nil {
			return e
		}
		if e = ctlAdmin(ctx, c, "/v1/suspend", map[string]string{"reason": "image upgrade"}, &struct{}{}); e != nil {
			return e
		}
		if e = setHelperImage(*helperPath, *image); e != nil {
			return e
		}
		if e = fixedService("restart", "personal-bot-helper.service"); e != nil {
			return e
		}
		if e = ctlAdmin(ctx, c, "/v1/resume", struct{}{}, &struct{}{}); e != nil {
			return e
		}
		var status map[string]any
		if e = ctlAdmin(ctx, c, "/v1/retry", struct{}{}, &status); e != nil {
			return fmt.Errorf("upgrade readiness failed; restore backup %s with rollback: %w", current.ID, e)
		}
		result = map[string]any{"upgraded": true, "image": *image, "previous_image": helperConfig.Image, "backup": current, "status": status}
	default:
		return errors.New("unknown administrative command")
	}
	encoder := json.NewEncoder(out)
	encoder.SetIndent("", "  ")
	return encoder.Encode(result)
}
