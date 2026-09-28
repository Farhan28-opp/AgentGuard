package main

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"io"
	"log/slog"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hyperledger/fabric-gateway/pkg/client"
	"github.com/hyperledger/fabric-gateway/pkg/hash"
	"github.com/hyperledger/fabric-gateway/pkg/identity"
	"github.com/hyperledger/fabric-protos-go-apiv2/common"
	"github.com/hyperledger/fabric-protos-go-apiv2/gateway"
	"github.com/hyperledger/fabric-protos-go-apiv2/peer"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
)

// ── fake Drunix Gateway (gRPC) ──────────────────────────────────────────────

type fakeGateway struct {
	gateway.UnimplementedGatewayServer
	mu           sync.Mutex
	result       []byte
	endorseErr   error
	submitErr    error
	statusErr    error
	statusDelay  time.Duration
	commitCode   peer.TxValidationCode
	block        uint64
	statusCalls  int
	evaluateErr  error
	evaluateResp []byte
}

func mustMarshal(m proto.Message) []byte {
	b, err := proto.Marshal(m)
	if err != nil {
		panic(err)
	}
	return b
}

func envelopeWithResult(channel string, result []byte) *common.Envelope {
	action := &peer.ChaincodeAction{Response: &peer.Response{Status: 200, Payload: result}}
	prp := &peer.ProposalResponsePayload{Extension: mustMarshal(action)}
	cap := &peer.ChaincodeActionPayload{Action: &peer.ChaincodeEndorsedAction{ProposalResponsePayload: mustMarshal(prp)}}
	tx := &peer.Transaction{Actions: []*peer.TransactionAction{{Payload: mustMarshal(cap)}}}
	payload := &common.Payload{
		Header: &common.Header{ChannelHeader: mustMarshal(&common.ChannelHeader{ChannelId: channel})},
		Data:   mustMarshal(tx),
	}
	return &common.Envelope{Payload: mustMarshal(payload)}
}

func (f *fakeGateway) Endorse(ctx context.Context, req *gateway.EndorseRequest) (*gateway.EndorseResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.endorseErr != nil {
		return nil, f.endorseErr
	}
	return &gateway.EndorseResponse{PreparedTransaction: envelopeWithResult("mychannel", f.result)}, nil
}

func (f *fakeGateway) Submit(ctx context.Context, req *gateway.SubmitRequest) (*gateway.SubmitResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.submitErr != nil {
		return nil, f.submitErr
	}
	return &gateway.SubmitResponse{}, nil
}

func (f *fakeGateway) CommitStatus(ctx context.Context, req *gateway.SignedCommitStatusRequest) (*gateway.CommitStatusResponse, error) {
	f.mu.Lock()
	f.statusCalls++
	delay, err, code, block := f.statusDelay, f.statusErr, f.commitCode, f.block
	f.mu.Unlock()
	if delay > 0 {
		select {
		case <-time.After(delay):
		case <-ctx.Done():
			return nil, status.FromContextError(ctx.Err()).Err()
		}
	}
	if err != nil {
		return nil, err
	}
	return &gateway.CommitStatusResponse{Result: code, BlockNumber: block}, nil
}

func (f *fakeGateway) Evaluate(ctx context.Context, req *gateway.EvaluateRequest) (*gateway.EvaluateResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.evaluateErr != nil {
		return nil, f.evaluateErr
	}
	return &gateway.EvaluateResponse{Result: &peer.Response{Status: 200, Payload: f.evaluateResp}}, nil
}

func testIdentity(t *testing.T) (*identity.X509Identity, identity.Sign) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tpl := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "User1@org1.example.com"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour)}
	der, err := x509.CreateCertificate(rand.Reader, tpl, tpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, _ := x509.ParseCertificate(der)
	id, err := identity.NewX509Identity("Org1MSP", cert)
	if err != nil {
		t.Fatal(err)
	}
	sign, err := identity.NewPrivateKeySign(key)
	if err != nil {
		t.Fatal(err)
	}
	return id, sign
}

// newRealLedger wires the production gatewayLedger to the fake gRPC Gateway.
func newRealLedger(t *testing.T, fg *fakeGateway, commitTimeout time.Duration) *gatewayLedger {
	lis := bufconn.Listen(1 << 20)
	gs := grpc.NewServer()
	gateway.RegisterGatewayServer(gs, fg)
	go gs.Serve(lis)
	t.Cleanup(gs.Stop)
	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return lis.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	id, sign := testIdentity(t)
	gw, err := client.Connect(id, client.WithSign(sign), client.WithHash(hash.SHA256), client.WithClientConnection(conn))
	if err != nil {
		t.Fatal(err)
	}
	return &gatewayLedger{
		contract: gw.GetNetwork("mychannel").GetContract("agentauth"),
		ready:    func() (bool, string) { return true, "READY" },
		timeouts: Timeouts{Evaluate: 2 * time.Second, Endorse: 2 * time.Second, Submit: 2 * time.Second, CommitStatus: commitTimeout},
	}
}

func newTestServer(l Ledger, token string) *httptest.Server {
	s := &Server{ledger: l, channel: "mychannel", chaincode: "agentauth", peer: "dns:///localhost:7051", msp: "Org1MSP",
		token: token, log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	return httptest.NewServer(s.Handler())
}

func post(t *testing.T, url, token, body string) (int, map[string]interface{}) {
	req, _ := http.NewRequest(http.MethodPost, url, bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("response is not JSON: %v", err)
	}
	return resp.StatusCode, out
}

func errField(out map[string]interface{}, k string) interface{} {
	e, _ := out["error"].(map[string]interface{})
	return e[k]
}

const reserveBody = `{"function":"Reserve","args":["PC1","R1","agent","100","INR","quickkart","groceries","k","90"]}`

// ── tests through the real fabric-gateway client ────────────────────────────

func TestSubmitValidCommit(t *testing.T) {
	fg := &fakeGateway{result: []byte(`{"reservation":{"id":"R1"}}`), commitCode: peer.TxValidationCode_VALID, block: 42}
	srv := newTestServer(newRealLedger(t, fg, 2*time.Second), "")
	defer srv.Close()
	code, out := post(t, srv.URL+"/v1/submit", "", reserveBody)
	if code != 200 || out["ok"] != true || out["status"] != "VALID" || out["block_number"].(float64) != 42 {
		t.Fatalf("unexpected: %d %v", code, out)
	}
	if out["tx_id"] == "" || fg.statusCalls != 1 {
		t.Fatalf("bridge must wait for commit status: calls=%d out=%v", fg.statusCalls, out)
	}
	if res := out["result"].(map[string]interface{}); res["reservation"] == nil {
		t.Fatalf("result not passed through: %v", out)
	}
}

func TestSubmitMVCCConflictIsNotSuccess(t *testing.T) {
	fg := &fakeGateway{result: []byte(`{}`), commitCode: peer.TxValidationCode_MVCC_READ_CONFLICT, block: 43}
	srv := newTestServer(newRealLedger(t, fg, 2*time.Second), "")
	defer srv.Close()
	code, out := post(t, srv.URL+"/v1/submit", "", reserveBody)
	if code != http.StatusConflict || out["ok"] != false || errField(out, "category") != CatMVCCConflict {
		t.Fatalf("unexpected: %d %v", code, out)
	}
	if errField(out, "tx_id") == "" || errField(out, "block_number").(float64) != 43 {
		t.Fatalf("MVCC failure must carry tx id and block: %v", out)
	}
}

func TestSubmitOtherInvalidCommit(t *testing.T) {
	fg := &fakeGateway{result: []byte(`{}`), commitCode: peer.TxValidationCode_ENDORSEMENT_POLICY_FAILURE, block: 44}
	srv := newTestServer(newRealLedger(t, fg, 2*time.Second), "")
	defer srv.Close()
	code, out := post(t, srv.URL+"/v1/submit", "", reserveBody)
	if code != http.StatusBadGateway || errField(out, "category") != CatInvalidCommit ||
		errField(out, "validation_code") != "ENDORSEMENT_POLICY_FAILURE" {
		t.Fatalf("unexpected: %d %v", code, out)
	}
}

func TestSubmitChaincodeRejection(t *testing.T) {
	fg := &fakeGateway{endorseErr: status.Error(codes.Aborted,
		"failed to endorse transaction: chaincode response 500, AGENTAUTH:INSUFFICIENT_AUTHORITY: capability PC1 has 300000 paise unallocated; reserve asks for 900000")}
	srv := newTestServer(newRealLedger(t, fg, 2*time.Second), "")
	defer srv.Close()
	code, out := post(t, srv.URL+"/v1/submit", "", reserveBody)
	if code != http.StatusConflict || errField(out, "category") != CatChaincodeRejected ||
		errField(out, "code") != "INSUFFICIENT_AUTHORITY" || errField(out, "stage") != StageEndorse {
		t.Fatalf("unexpected: %d %v", code, out)
	}
	if !strings.Contains(errField(out, "message").(string), "300000 paise") {
		t.Fatalf("rejection detail lost: %v", out)
	}
	if fg.statusCalls != 0 {
		t.Fatalf("rejected endorsement must never be submitted")
	}
}

func TestSubmitCommitStatusTimeout(t *testing.T) {
	fg := &fakeGateway{result: []byte(`{}`), commitCode: peer.TxValidationCode_VALID, statusDelay: 3 * time.Second}
	srv := newTestServer(newRealLedger(t, fg, 200*time.Millisecond), "")
	defer srv.Close()
	code, out := post(t, srv.URL+"/v1/submit", "", reserveBody)
	if code != http.StatusGatewayTimeout || errField(out, "category") != CatTimeout || errField(out, "stage") != StageCommitStatus {
		t.Fatalf("unexpected: %d %v", code, out)
	}
	if errField(out, "tx_id") == "" {
		t.Fatalf("timeout must report the tx id so the outcome can be reconciled: %v", out)
	}
}

func TestSubmitUnavailable(t *testing.T) {
	fg := &fakeGateway{submitErr: status.Error(codes.Unavailable, "orderer unavailable")}
	srv := newTestServer(newRealLedger(t, fg, 2*time.Second), "")
	defer srv.Close()
	code, out := post(t, srv.URL+"/v1/submit", "", reserveBody)
	if code != http.StatusServiceUnavailable || errField(out, "category") != CatUnavailable || errField(out, "stage") != StageSubmit {
		t.Fatalf("unexpected: %d %v", code, out)
	}
}

func TestEvaluateAndHealth(t *testing.T) {
	fg := &fakeGateway{evaluateResp: []byte(`{"contract":"agentauth","version":"1.0.0"}`)}
	srv := newTestServer(newRealLedger(t, fg, 2*time.Second), "")
	defer srv.Close()
	code, out := post(t, srv.URL+"/v1/evaluate", "", `{"function":"GetCapability","args":["PC1"]}`)
	if code != 200 || out["ok"] != true {
		t.Fatalf("unexpected: %d %v", code, out)
	}
	resp, err := http.Get(srv.URL + "/health")
	if err != nil {
		t.Fatal(err)
	}
	var h map[string]interface{}
	_ = json.NewDecoder(resp.Body).Decode(&h)
	resp.Body.Close()
	if resp.StatusCode != 200 || h["status"] != "ok" || h["chaincode_status"] != "ready" {
		t.Fatalf("unexpected health: %d %v", resp.StatusCode, h)
	}
}

func TestHealthDegradedWhenDrunixDown(t *testing.T) {
	fg := &fakeGateway{evaluateErr: status.Error(codes.Unavailable, "connection refused")}
	srv := newTestServer(newRealLedger(t, fg, 2*time.Second), "")
	defer srv.Close()
	resp, err := http.Get(srv.URL + "/health")
	if err != nil {
		t.Fatal(err)
	}
	var h map[string]interface{}
	_ = json.NewDecoder(resp.Body).Decode(&h)
	resp.Body.Close()
	if resp.StatusCode != 503 || h["status"] != "degraded" || h["drunix"] != "unreachable" {
		t.Fatalf("health must not claim success when Drunix is down: %d %v", resp.StatusCode, h)
	}
}

// ── request validation, auth, malformed data ────────────────────────────────

type stubLedger struct{ out []byte }

func (s stubLedger) Submit(context.Context, string, []string) (*SubmitResult, error) {
	return &SubmitResult{TransactionID: "tx1", BlockNumber: 9, ValidationCode: "VALID", Result: s.out}, nil
}
func (s stubLedger) Evaluate(context.Context, string, []string) ([]byte, error) { return s.out, nil }
func (s stubLedger) Ready() (bool, string)                                      { return true, "READY" }

func TestMalformedRequests(t *testing.T) {
	srv := newTestServer(stubLedger{out: []byte(`{}`)}, "")
	defer srv.Close()
	for _, body := range []string{
		`not json`,
		`{"function":""}`,
		`{"function":"DeleteEverything","args":[]}`,
		`{"function":"Reserve","args":"PC1"}`,
		`{"function":"Reserve","args":[],"extra":1}`,
	} {
		code, out := post(t, srv.URL+"/v1/submit", "", body)
		if code != 400 || errField(out, "category") != CatBadRequest {
			t.Fatalf("body %q: unexpected %d %v", body, code, out)
		}
	}
	// read functions are not accepted on the submit endpoint and vice versa
	if code, _ := post(t, srv.URL+"/v1/submit", "", `{"function":"GetCapability","args":["x"]}`); code != 400 {
		t.Fatalf("GetCapability must not be submittable")
	}
	if code, _ := post(t, srv.URL+"/v1/evaluate", "", `{"function":"Reserve","args":[]}`); code != 400 {
		t.Fatalf("Reserve must not be evaluable")
	}
}

func TestNonJSONChaincodeResultIsWrapped(t *testing.T) {
	srv := newTestServer(stubLedger{out: []byte("plain text")}, "")
	defer srv.Close()
	code, out := post(t, srv.URL+"/v1/submit", "", reserveBody)
	if code != 200 || out["result"] != "plain text" {
		t.Fatalf("unexpected: %d %v", code, out)
	}
}

func TestTokenRequired(t *testing.T) {
	srv := newTestServer(stubLedger{out: []byte(`{}`)}, "s3cret")
	defer srv.Close()
	if code, out := post(t, srv.URL+"/v1/submit", "", reserveBody); code != 401 || errField(out, "category") != CatUnauthorized {
		t.Fatalf("missing token must be refused: %d", code)
	}
	if code, _ := post(t, srv.URL+"/v1/submit", "wrong", reserveBody); code != 401 {
		t.Fatalf("wrong token must be refused")
	}
	if code, _ := post(t, srv.URL+"/v1/submit", "s3cret", reserveBody); code != 200 {
		t.Fatalf("valid token must be accepted")
	}
}

func TestClassify(t *testing.T) {
	cases := []struct {
		stage string
		err   error
		cat   string
		code  string
	}{
		{StageEndorse, status.Error(codes.Aborted, "chaincode response 500, AGENTAUTH:ALREADY_COMMITTED: reservation R1 was already committed"), CatChaincodeRejected, "ALREADY_COMMITTED"},
		{StageEndorse, status.Error(codes.Unavailable, "no endorsers"), CatUnavailable, "Unavailable"},
		{StageEndorse, status.Error(codes.Aborted, "chaincode agentauth not found"), CatChaincodeRejected, "Aborted"},
		{StageSubmit, status.Error(codes.DeadlineExceeded, "slow orderer"), CatTimeout, "DeadlineExceeded"},
		{StageCommitStatus, context.DeadlineExceeded, CatTimeout, "DeadlineExceeded"},
		{StageSubmit, io.ErrUnexpectedEOF, CatInternal, "UNKNOWN"},
	}
	for _, c := range cases {
		le := classify(c.stage, "tx", c.err)
		if le.Category != c.cat || le.Code != c.code {
			t.Errorf("%v: got %s/%s want %s/%s", c.err, le.Category, le.Code, c.cat, c.code)
		}
	}
	if le := fromValidationCode("tx", 1, peer.TxValidationCode_PHANTOM_READ_CONFLICT); le.Category != CatMVCCConflict {
		t.Errorf("phantom read must be treated as a concurrency conflict")
	}
}
