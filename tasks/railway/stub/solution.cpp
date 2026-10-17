// Starting point: compiles and links, but implements nothing.
// Replace it with your engine. See TASK.md.
#include "ticket_system.h"

namespace rail {

namespace {

class StubSystem final : public TicketSystem {
 public:
  void init(const WorldData&) override {}

  Status query(const QueryRequest&, QueryResult* out) override {
    out->count = 0;
    return Status::Ok;
  }

  Status book(const BookRequest&, BookResult*) override { return Status::SoldOut; }

  Status refund(uint64_t) override { return Status::NotFound; }
};

}  // namespace

TicketSystem* create_ticket_system() { return new StubSystem(); }

}  // namespace rail
