// Package scheduler 轻量任务派发:把 job 行写入 DB 后向 worker 提交任务。
package scheduler

import (
	"context"
	"encoding/json"
	"fmt"

	"dsh/openai-register-gateway/internal/worker"
)

type Scheduler struct {
	Worker *worker.Client
}

func New(w *worker.Client) *Scheduler {
	return &Scheduler{Worker: w}
}

// Dispatch 向 worker 提交任务并记录 task_id(任务明细表由各 API handler 维护)。
func (s *Scheduler) Dispatch(ctx context.Context, taskID, kind string, params interface{}) error {
	raw, err := json.Marshal(params)
	if err != nil {
		return err
	}
	return s.Worker.Submit(ctx, &worker.Task{TaskID: taskID, Kind: kind, Params: raw})
}

func (s *Scheduler) Status(ctx context.Context, taskID string) (string, error) {
	st, err := s.Worker.Status(ctx, taskID)
	if err != nil {
		return "", err
	}
	return st.Status, nil
}

var _ = fmt.Sprintf
