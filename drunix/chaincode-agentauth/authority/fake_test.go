package authority

import (
	"crypto/x509"
	"fmt"
	"testing"

	"github.com/hyperledger/fabric-chaincode-go/v2/pkg/cid"
	"github.com/hyperledger/fabric-chaincode-go/v2/shim"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// fakeStub is an in-memory world state with Fabric's transactional semantics:
// writes are buffered per transaction and only applied when the transaction
// function returns without error (a rejected transaction changes nothing).
type fakeStub struct {
	shim.ChaincodeStubInterface
	state   map[string][]byte
	pending map[string][]byte
	txID    string
	now     int64
	txSeq   int
	events  []string
}

func newFakeStub(now int64) *fakeStub {
	return &fakeStub{state: map[string][]byte{}, now: now}
}

func (s *fakeStub) begin() {
	s.txSeq++
	s.txID = fmt.Sprintf("tx%04d", s.txSeq)
	s.pending = map[string][]byte{}
}

func (s *fakeStub) end(ok bool) {
	if ok {
		for k, v := range s.pending {
			if v == nil {
				delete(s.state, k)
			} else {
				s.state[k] = v
			}
		}
	}
	s.pending = nil
}

func (s *fakeStub) GetState(key string) ([]byte, error) {
	if s.pending != nil {
		if v, ok := s.pending[key]; ok {
			return v, nil
		}
	}
	return s.state[key], nil
}

func (s *fakeStub) PutState(key string, value []byte) error {
	if s.pending == nil {
		return fmt.Errorf("PutState outside a transaction")
	}
	s.pending[key] = append([]byte(nil), value...)
	return nil
}

func (s *fakeStub) DelState(key string) error {
	s.pending[key] = nil
	return nil
}

func (s *fakeStub) GetTxID() string { return s.txID }

func (s *fakeStub) GetTxTimestamp() (*timestamppb.Timestamp, error) {
	return &timestamppb.Timestamp{Seconds: s.now}, nil
}

func (s *fakeStub) SetEvent(name string, payload []byte) error {
	s.events = append(s.events, name)
	return nil
}

type fakeIdentity struct{ msp string }

func (f fakeIdentity) GetID() (string, error)    { return "x509::CN=User1@org1.example.com", nil }
func (f fakeIdentity) GetMSPID() (string, error) { return f.msp, nil }
func (f fakeIdentity) GetAttributeValue(string) (string, bool, error) {
	return "", false, nil
}
func (f fakeIdentity) AssertAttributeValue(string, string) error      { return nil }
func (f fakeIdentity) GetX509Certificate() (*x509.Certificate, error) { return nil, nil }

var _ cid.ClientIdentity = fakeIdentity{}

type fakeCtx struct {
	stub *fakeStub
	id   fakeIdentity
}

func (c *fakeCtx) GetStub() shim.ChaincodeStubInterface  { return c.stub }
func (c *fakeCtx) GetClientIdentity() cid.ClientIdentity { return c.id }

// harness wraps the contract and runs every call as one transaction.
type harness struct {
	t   *testing.T
	c   *Contract
	ctx *fakeCtx
}

func newHarness(t *testing.T) *harness {
	return &harness{t: t, c: &Contract{}, ctx: &fakeCtx{stub: newFakeStub(1_800_000_000), id: fakeIdentity{msp: "Org1MSP"}}}
}

func tx[T any](h *harness, fn func() (T, error)) (T, error) {
	h.ctx.stub.begin()
	out, err := fn()
	h.ctx.stub.end(err == nil)
	return out, err
}
