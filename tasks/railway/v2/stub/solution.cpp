#include "ticket_system_v2.h"
namespace rail::v2 {
class Empty final : public TicketSystem {
 public:
  void init(const WorldData&, const Options&) override {}
  Status query(const QueryRequest&, QueryResult*) override { return Status::Invalid; }
  Status book(const BookRequest&, BookResult*) override { return Status::Invalid; }
  Status refund(const RefundRequest&) override { return Status::NotFound; }
  Status exchange(const ExchangeRequest&, BookResult*) override { return Status::Invalid; }
};
TicketSystem* create_ticket_system() { return new Empty(); }
}
