//go:build linux

package host

import (
	"errors"
	"os"
	"syscall"
)

// The helper checks every fixed path component after confirming the runtime
// is absent. Descriptor-based changes cannot follow a final-component link.
func validateOfflineSnapshotRoot(path string) error {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != 0 || info.Mode().Perm()&0022 != 0 {
		return errors.New("offline snapshot root must be root controlled")
	}
	return nil
}

func prepareOfflineSnapshotDirectory(path string, create bool) error {
	if create {
		if err := os.Mkdir(path, 0700); err != nil && !os.IsExist(err) {
			return err
		}
	}
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || (stat.Uid != 0 && stat.Uid != 31001) || info.Mode().Perm()&0022 != 0 {
		return errors.New("offline snapshot directory has unsafe ownership or permissions")
	}
	// UID 0 is accepted only to repair paths made by the prior offline helper.
	if err = f.Chown(31001, 31000); err != nil {
		return err
	}
	return f.Chmod(0700)
}

func prepareOfflineSnapshotFile(path string) error {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || !info.Mode().IsRegular() || stat.Nlink != 1 || (stat.Uid != 0 && stat.Uid != 31001) || info.Mode().Perm()&0022 != 0 {
		return errors.New("offline snapshot database has unsafe type or ownership")
	}
	if err = f.Chown(31001, 31000); err != nil {
		return err
	}
	return f.Chmod(0600)
}
