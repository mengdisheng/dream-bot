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
	p := flag.String("config", "/etc/personal-bot/helper.json", "root-owned fixed runtime template")
	flag.Parse()
	c, e := host.LoadHelperConfig(*p)
	if e == nil {
		ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		defer cancel()
		e = host.ServeHelper(ctx, c)
	}
	if e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(1)
	}
}
