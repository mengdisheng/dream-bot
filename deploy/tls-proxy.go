// The test listener is loopback-only. SSH forwarding supplies remote access.
package main

import (
    "crypto/tls"
    "log"
    "net/http"
    "net/http/httputil"
    "net/url"
    "os"
    "time"
)

func main() {
    if len(os.Args)!=3 { log.Fatal("usage: personal-bot-tls CERT KEY") }
    target,_:=url.Parse("http://127.0.0.1:8090")
    proxy:=httputil.NewSingleHostReverseProxy(target)
    original:=proxy.Director
    proxy.Director=func(r *http.Request) { original(r); r.Header.Set("X-Forwarded-Proto","https") }
    proxy.ErrorHandler=func(w http.ResponseWriter,r *http.Request,err error) { http.Error(w,"入口暂不可用",http.StatusBadGateway) }
    server:=&http.Server{Addr:"127.0.0.1:8443",Handler:proxy,ReadHeaderTimeout:10*time.Second,MaxHeaderBytes:65536,
        TLSConfig:&tls.Config{MinVersion:tls.VersionTLS13}}
    log.Fatal(server.ListenAndServeTLS(os.Args[1],os.Args[2]))
}
