package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	host "personalbot.local/host"
	"syscall"
)

func main() {
	p := flag.String("config", "/etc/personal-bot/host.json", "root-provisioned gateway config")
	flag.Parse()
	c, e := host.LoadConfig(*p)
	if e == nil {
		ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		defer cancel()
		e = host.ServeGateway(ctx, c)
	}
	if e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(1)
	}
}
