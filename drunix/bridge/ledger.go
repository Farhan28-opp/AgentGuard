package main

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/hyperledger/fabric-gateway/pkg/client"
	"github.com/hyperledger/fabric-protos-go-apiv2/peer"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Error categories returned to AgentGuard.
const (
	CatChaincodeRejected = "CHAINCODE_REJECTED"
	CatMVCCConflict      = "MVCC_READ_CONFLICT"
	CatUnavailable       = "DRUNIX_UNAVAILABLE"
	CatTimeout           = "DRUNIX_TIMEOUT"
	CatInvalidCommit     = "DRUNIX_INVALID_COMMIT"
	CatBadRequest        = "BAD_REQUEST"
	CatUnauthorized      = "UNAUTHORIZED"
	CatInternal          = "BRIDGE_ERROR"
)

// Stages of a Drunix transaction.
const (
	StageEndorse      = "endorse"
	StageSubmit       = "submit"
	StageCommitStatus = "commit_status"
	StageCommit       = "commit"
	StageEvaluate     = "evaluate"
)

// SubmitResult describes a transaction that was committed as VALID.
type SubmitResult struct {
	TransactionID  string
	BlockNumber    uint64
	ValidationCode string
	Result         []byte
	EndorseMs      int64
	CommitMs       int64
}

// LedgerError is a classified failure of a Drunix operation.
type LedgerError struct {
	Category       string
	Stage          string
	Code           string // agentauth rejection code, gRPC code or validation code
	Message        string
	TransactionID  string
	BlockNumber    uint64
	ValidationCode string
}

func (e *LedgerError) Error() string {
	return fmt.Sprintf("%s at %s: %s", e.Category, e.Stage, e.Message)
}

// Ledger is the operation set the HTTP layer needs; the real implementation
// talks to the Drunix Gateway, tests use a fake.
type Ledger interface {
	Submit(ctx context.Context, fn string, args []string) (*SubmitResult, error)
	Evaluate(ctx context.Context, fn string, args []string) ([]byte, error)
	Ready() (bool, string)
}

var rejectionRe = regexp.MustCompile(`AGENTAUTH:([A-Z_]+): ([^\n]*)`)

// classify turns any error from the Gateway into a LedgerError.
func classify(stage string, txID string, err error) *LedgerError {
	var le *LedgerError
	if errors.As(err, &le) {
		return le
	}
	out := &LedgerError{Stage: stage, TransactionID: txID, Message: err.Error()}

	// A chaincode rejection carries agentauth's stable error code.
	if m := rejectionRe.FindStringSubmatch(err.Error()); m != nil {
		out.Category, out.Code, out.Message = CatChaincodeRejected, m[1], strings.TrimSpace(m[2])
		return out
	}

	var commitErr *client.CommitError
	if errors.As(err, &commitErr) {
		return fromValidationCode(commitErr.TransactionID, 0, commitErr.Code)
	}

	if errors.Is(err, context.DeadlineExceeded) {
		out.Category, out.Code = CatTimeout, codes.DeadlineExceeded.String()
		return out
	}
	if errors.Is(err, context.Canceled) {
		out.Category, out.Code = CatTimeout, codes.Canceled.String()
		return out
	}

	st, ok := status.FromError(err)
	if !ok {
		out.Category, out.Code = CatInternal, "UNKNOWN"
		return out
	}
	out.Code = st.Code().String()
	switch st.Code() {
	case codes.DeadlineExceeded, codes.Canceled:
		out.Category = CatTimeout
	case codes.Unavailable, codes.ResourceExhausted:
		out.Category = CatUnavailable
	case codes.Aborted, codes.Unknown, codes.FailedPrecondition, codes.InvalidArgument, codes.NotFound, codes.PermissionDenied:
		// Endorsement failures that are not agentauth rejections (for example a
		// chaincode that is not deployed, or a function that does not exist).
		if stage == StageEndorse || stage == StageEvaluate {
			out.Category = CatChaincodeRejected
		} else {
			out.Category = CatUnavailable
		}
	default:
		out.Category = CatUnavailable
	}
	return out
}

// fromValidationCode maps a non-VALID commit status.
func fromValidationCode(txID string, block uint64, code peer.TxValidationCode) *LedgerError {
	name := peer.TxValidationCode_name[int32(code)]
	out := &LedgerError{
		Stage: StageCommit, TransactionID: txID, BlockNumber: block, Code: name, ValidationCode: name,
		Message: fmt.Sprintf("transaction %s was committed as %s, not VALID", txID, name),
	}
	switch code {
	case peer.TxValidationCode_MVCC_READ_CONFLICT, peer.TxValidationCode_PHANTOM_READ_CONFLICT:
		out.Category = CatMVCCConflict
		out.Message = fmt.Sprintf("transaction %s lost a concurrent-update race (%s); the ledger state it read had changed", txID, name)
	default:
		out.Category = CatInvalidCommit
	}
	return out
}

// Timeouts for each Gateway call.
type Timeouts struct {
	Evaluate     time.Duration
	Endorse      time.Duration
	Submit       time.Duration
	CommitStatus time.Duration
}

// gatewayLedger submits transactions through the Drunix Gateway (Lite Peer) and
// waits for the Committing Peer's verdict before reporting success.
type gatewayLedger struct {
	contract *client.Contract
	ready    func() (bool, string)
	timeouts Timeouts
}

func (g *gatewayLedger) Ready() (bool, string) { return g.ready() }

func (g *gatewayLedger) Evaluate(ctx context.Context, fn string, args []string) ([]byte, error) {
	cctx, cancel := context.WithTimeout(ctx, g.timeouts.Evaluate)
	defer cancel()
	proposal, err := g.contract.NewProposal(fn, client.WithArguments(args...))
	if err != nil {
		return nil, classify(StageEvaluate, "", err)
	}
	out, err := proposal.EvaluateWithContext(cctx)
	if err != nil {
		return nil, classify(StageEvaluate, proposal.TransactionID(), err)
	}
	return out, nil
}

func (g *gatewayLedger) Submit(ctx context.Context, fn string, args []string) (*SubmitResult, error) {
	proposal, err := g.contract.NewProposal(fn, client.WithArguments(args...))
	if err != nil {
		return nil, classify(StageEndorse, "", err)
	}
	txID := proposal.TransactionID()

	t0 := time.Now()
	ectx, cancel := context.WithTimeout(ctx, g.timeouts.Endorse)
	endorsed, err := proposal.EndorseWithContext(ectx)
	cancel()
	if err != nil {
		return nil, classify(StageEndorse, txID, err)
	}
	endorseMs := time.Since(t0).Milliseconds()

	t1 := time.Now()
	sctx, cancel := context.WithTimeout(ctx, g.timeouts.Submit)
	commit, err := endorsed.SubmitWithContext(sctx)
	cancel()
	if err != nil {
		return nil, classify(StageSubmit, txID, err)
	}

	// Never report success because Submit() returned: wait for the Committing
	// Peer's validation result (the Lite Peer forwards this request).
	cctx, cancel := context.WithTimeout(ctx, g.timeouts.CommitStatus)
	st, err := commit.StatusWithContext(cctx)
	cancel()
	if err != nil {
		return nil, classify(StageCommitStatus, txID, err)
	}
	if !st.Successful || st.Code != peer.TxValidationCode_VALID {
		return nil, fromValidationCode(txID, st.BlockNumber, st.Code)
	}
	return &SubmitResult{
		TransactionID:  txID,
		BlockNumber:    st.BlockNumber,
		ValidationCode: peer.TxValidationCode_VALID.String(),
		Result:         endorsed.Result(),
		EndorseMs:      endorseMs,
		CommitMs:       time.Since(t1).Milliseconds(),
	}, nil
}
