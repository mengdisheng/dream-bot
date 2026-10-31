//go:build !linux

package host

import "errors"

func validateOfflineSnapshotRoot(string) error {
	return errors.New("offline snapshot ownership validation requires Linux")
}
func prepareOfflineSnapshotDirectory(string, bool) error {
	return errors.New("offline snapshot ownership validation requires Linux")
}
func prepareOfflineSnapshotFile(string) error {
	return errors.New("offline snapshot ownership validation requires Linux")
}
