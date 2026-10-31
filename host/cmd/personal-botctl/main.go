package main

import (
	"fmt"
	"os"
	host "personalbot.local/host"
)

func main() {
	if e := host.RunCTL(os.Args[1:], os.Stdin, os.Stdout, os.Stderr); e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(1)
	}
}
