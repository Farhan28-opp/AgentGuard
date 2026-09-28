// Command drunix-bridge exposes AgentGuard's Drunix operations over a small
// local HTTP API and forwards them to the Drunix network through the Fabric
// Gateway (the pattern used by drunix-network/asset-transfer-basic/
// application-gateway-go and rest-api-go).
//
// Credentials are read at runtime from the test network's generated
// organizations/ directory; nothing is embedded or logged.
package main

import (
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"syscall"
	"time"

	"github.com/hyperledger/fabric-gateway/pkg/client"
	"github.com/hyperledger/fabric-gateway/pkg/hash"
	"github.com/hyperledger/fabric-gateway/pkg/identity"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
)

type config struct {
	Listen       string
	Token        string
	Channel      string
	Chaincode    string
	MSPID        string
	PeerEndpoint string
	GatewayPeer  string
	CertPath     string
	KeyPath      string
	TLSCertPath  string
	Timeouts     Timeouts
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envDuration(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}

func loadConfig() (*config, error) {
	org := os.Getenv("DRUNIX_ORG_PATH") // .../test-network/organizations/peerOrganizations/org1.example.com
	user := env("DRUNIX_USER", "User1@org1.example.com")
	peer := env("DRUNIX_GATEWAY_PEER", "peer0.org1.example.com")
	c := &config{
		Listen:       env("BRIDGE_LISTEN", "127.0.0.1:8090"),
		Token:        os.Getenv("BRIDGE_TOKEN"),
		Channel:      env("DRUNIX_CHANNEL", "mychannel"),
		Chaincode:    env("DRUNIX_CHAINCODE", "agentauth"),
		MSPID:        env("DRUNIX_MSP_ID", "Org1MSP"),
		PeerEndpoint: env("DRUNIX_PEER_ENDPOINT", "dns:///localhost:7051"),
		GatewayPeer:  peer,
		CertPath:     os.Getenv("DRUNIX_CERT_PATH"),
		KeyPath:      os.Getenv("DRUNIX_KEY_PATH"),
		TLSCertPath:  os.Getenv("DRUNIX_TLS_CERT_PATH"),
		Timeouts: Timeouts{
			Evaluate:     envDuration("DRUNIX_EVALUATE_TIMEOUT", 10*time.Second),
			Endorse:      envDuration("DRUNIX_ENDORSE_TIMEOUT", 20*time.Second),
			Submit:       envDuration("DRUNIX_SUBMIT_TIMEOUT", 10*time.Second),
			CommitStatus: envDuration("DRUNIX_COMMIT_TIMEOUT", 60*time.Second),
		},
	}
	if org != "" {
		if c.CertPath == "" {
			c.CertPath = filepath.Join(org, "users", user, "msp", "signcerts")
		}
		if c.KeyPath == "" {
			c.KeyPath = filepath.Join(org, "users", user, "msp", "keystore")
		}
		if c.TLSCertPath == "" {
			c.TLSCertPath = filepath.Join(org, "peers", peer, "tls", "ca.crt")
		}
	}
	if c.CertPath == "" || c.KeyPath == "" || c.TLSCertPath == "" {
		return nil, errors.New("set DRUNIX_ORG_PATH (or DRUNIX_CERT_PATH, DRUNIX_KEY_PATH and DRUNIX_TLS_CERT_PATH)")
	}
	return c, nil
}

// readPEM reads a file, or the first regular file in a directory (the MSP
// keystore and signcerts layout).
func readPEM(path string) ([]byte, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() {
		return os.ReadFile(path)
	}
	entries, err := os.ReadDir(path)
	if err != nil {
		return nil, err
	}
	var names []string
	for _, e := range entries {
		if e.Type().IsRegular() {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	if len(names) == 0 {
		return nil, fmt.Errorf("no files in %s", path)
	}
	return os.ReadFile(filepath.Join(path, names[0]))
}

func connect(c *config) (*client.Gateway, *grpc.ClientConn, error) {
	tlsPEM, err := readPEM(c.TLSCertPath)
	if err != nil {
		return nil, nil, fmt.Errorf("read TLS CA certificate: %w", err)
	}
	tlsCert, err := identity.CertificateFromPEM(tlsPEM)
	if err != nil {
		return nil, nil, fmt.Errorf("parse TLS CA certificate: %w", err)
	}
	pool := x509.NewCertPool()
	pool.AddCert(tlsCert)
	conn, err := grpc.NewClient(c.PeerEndpoint, grpc.WithTransportCredentials(credentials.NewClientTLSFromCert(pool, c.GatewayPeer)))
	if err != nil {
		return nil, nil, fmt.Errorf("create gRPC connection: %w", err)
	}

	certPEM, err := readPEM(c.CertPath)
	if err != nil {
		return nil, nil, fmt.Errorf("read client certificate: %w", err)
	}
	cert, err := identity.CertificateFromPEM(certPEM)
	if err != nil {
		return nil, nil, fmt.Errorf("parse client certificate: %w", err)
	}
	id, err := identity.NewX509Identity(c.MSPID, cert)
	if err != nil {
		return nil, nil, err
	}
	keyPEM, err := readPEM(c.KeyPath)
	if err != nil {
		return nil, nil, fmt.Errorf("read client private key: %w", err)
	}
	key, err := identity.PrivateKeyFromPEM(keyPEM)
	if err != nil {
		return nil, nil, fmt.Errorf("parse client private key: %w", err)
	}
	sign, err := identity.NewPrivateKeySign(key)
	if err != nil {
		return nil, nil, err
	}
	gw, err := client.Connect(id, client.WithSign(sign), client.WithHash(hash.SHA256), client.WithClientConnection(conn),
		client.WithEvaluateTimeout(c.Timeouts.Evaluate), client.WithEndorseTimeout(c.Timeouts.Endorse),
		client.WithSubmitTimeout(c.Timeouts.Submit), client.WithCommitStatusTimeout(c.Timeouts.CommitStatus))
	if err != nil {
		conn.Close()
		return nil, nil, err
	}
	return gw, conn, nil
}

func main() {
	log := slog.New(slog.NewTextHandler(os.Stdout, nil))
	cfg, err := loadConfig()
	if err != nil {
		log.Error("configuration error", "error", err)
		os.Exit(2)
	}
	gw, conn, err := connect(cfg)
	if err != nil {
		log.Error("cannot initialise Drunix gateway connection", "error", err)
		os.Exit(1)
	}
	defer gw.Close()
	defer conn.Close()

	ledger := &gatewayLedger{
		contract: gw.GetNetwork(cfg.Channel).GetContract(cfg.Chaincode),
		timeouts: cfg.Timeouts,
		ready: func() (bool, string) {
			st := conn.GetState().String()
			return st == "READY" || st == "IDLE", st
		},
	}
	srv := &Server{ledger: ledger, channel: cfg.Channel, chaincode: cfg.Chaincode, peer: cfg.PeerEndpoint,
		msp: cfg.MSPID, token: cfg.Token, log: log}
	httpSrv := &http.Server{Addr: cfg.Listen, Handler: srv.Handler(), ReadHeaderTimeout: 5 * time.Second}

	go func() {
		stop := make(chan os.Signal, 1)
		signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
		<-stop
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = httpSrv.Shutdown(ctx)
	}()

	log.Info("drunix-bridge listening", "listen", cfg.Listen, "peer", cfg.PeerEndpoint, "gateway_peer", cfg.GatewayPeer,
		"channel", cfg.Channel, "chaincode", cfg.Chaincode, "msp", cfg.MSPID, "auth", cfg.Token != "")
	if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Error("server error", "error", err)
		os.Exit(1)
	}
}
