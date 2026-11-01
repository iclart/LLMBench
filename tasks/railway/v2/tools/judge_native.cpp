// ABI 2 independent native judge. No contestant/strong-reference code is linked.
#include <algorithm>
#include <array>
#include <bit>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <functional>
#include <iostream>
#include <limits>
#include <memory>
#include <iomanip>
#include <numeric>
#include <map>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>
using namespace std;

// Read-only JSON, intentionally limited to the standard JSON value grammar.
struct J {
  enum Type { Null, Number, String, Array, Object, Boolean } type=Null;
  string digits; bool b=false; string s; vector<J> a; map<string,J> o;
  const J& operator[](const string& k) const {auto it=o.find(k);if(it==o.end())throw runtime_error("missing JSON key: "+k);return it->second;}
  const J& operator[](size_t k) const {return a.at(k);}
  bool has(const string& k)const{return o.count(k);}
  int64_t num()const {if(type!=Number)throw runtime_error("JSON integer expected");return stoll(digits);}
  uint64_t u64()const {if(type!=Number||digits.empty()||digits[0]=='-')throw runtime_error("JSON unsigned integer expected");return stoull(digits);}
  string str()const {if(type!=String)throw runtime_error("JSON string expected");return s;}
};
struct Parser {
 const string& s;size_t i=0;
 void ws(){while(i<s.size() && (s[i]==' '||s[i]=='\t'||s[i]=='\r'||s[i]=='\n'))++i;}
 char get(){if(i>=s.size())throw runtime_error("truncated JSON");return s[i++];}
 void expect(char c){ws();if(get()!=c)throw runtime_error("bad JSON delimiter");}
 string readstr(){string out;expect('"');while(true){char c=get();if(c=='"')break;if(c=='\\'){char e=get();if(e=='u'){for(int k=0;k<4;++k)get();out+='?';}else if(e=='n')out+='\n';else if(e=='r')out+='\r';else if(e=='t')out+='\t';else out+=e;}else out+=c;}return out;}
 J parse(){ws();char c=s.at(i);J v;
  if(c=='{'){v.type=J::Object;get();ws();if(s.at(i)=='}'){++i;return v;}while(true){auto k=readstr();expect(':');v.o.emplace(move(k),parse());ws();char end=get();if(end=='}')break;if(end!=',')throw runtime_error("bad object");}return v;}
  if(c=='['){v.type=J::Array;get();ws();if(s.at(i)==']'){++i;return v;}while(true){v.a.push_back(parse());ws();char end=get();if(end==']')break;if(end!=',')throw runtime_error("bad array");}return v;}
  if(c=='"'){v.type=J::String;v.s=readstr();return v;}
  if(c=='-' || (c>='0'&&c<='9')){v.type=J::Number;size_t b=i;get();while(i<s.size()&&s[i]>='0'&&s[i]<='9')++i;v.digits=s.substr(b,i-b);return v;}
  if(s.compare(i,4,"true")==0){i+=4;v.type=J::Boolean;v.b=true;return v;}
  if(s.compare(i,5,"false")==0){i+=5;v.type=J::Boolean;v.b=false;return v;}
  if(s.compare(i,4,"null")==0){i+=4;return v;}
  throw runtime_error("invalid JSON value");
 }
};
J json(const string& s){Parser p{s};auto v=p.parse();p.ws();if(p.i!=s.size())throw runtime_error("JSON trailer");return v;}
string escape(const string& s){string x="\"";for(char c:s){if(c=='\\'||c=='"'){x+='\\';x+=c;}else if(c=='\n')x+="\\n";else if(c=='\r')x+="\\r";else if(c=='\t')x+="\\t";else x+=c;}return x+'"';}
int I(const J& v){return int(v.num());}
using Leg=array<int,4>;using RouteLeg=array<int,6>;using Seat=array<int,3>;
struct Book{vector<Leg> legs;int cls=0,pax=0,window=0,together=0;};
struct Stop{int station=0,arrive=0,depart=0;};
struct World{int stations=0;vector<vector<Stop>> trains;};
World world_read(const string& path){ifstream f(path);if(!f)throw runtime_error("cannot open world");string tag;int v;f>>tag>>v;if(tag!="RAILWORLD"||(v!=1&&v!=2))throw runtime_error("invalid world header");World w;f>>tag>>w.stations;if(tag!="STATIONS")throw runtime_error("invalid stations");for(int i=0;i<w.stations;i++)f>>tag;int lines;f>>tag>>lines;if(tag!="LINES")throw runtime_error("invalid lines");for(int l=0;l<lines;l++){int n;f>>tag>>n;for(int i=0,x;i<n;i++)f>>x;}int trains;f>>tag>>trains;if(tag!="TRAINS")throw runtime_error("invalid trains");w.trains.resize(trains);for(auto& stops:w.trains){int numlines=v==2?0:1;if(v==2)f>>tag>>numlines;for(int i=0;i<numlines;i++)f>>tag;int n;f>>n;stops.resize(n);for(auto& stop:stops)f>>stop.station>>stop.arrive>>stop.depart;}f>>tag;if(!f||tag!="END")throw runtime_error("invalid world trailer");return w;}
Book readbook(const J& j){Book b;b.cls=I(j["class"]);b.pax=I(j["pax"]);if(j.has("window"))b.window=I(j["window"]);if(j.has("together"))b.together=I(j["together"]);for(const auto& l:j["legs"].a){if(l.a.size()!=4)throw runtime_error("bad leg");b.legs.push_back({I(l[0]),I(l[1]),I(l[2]),I(l[3])});}return b;}
struct Event{int idx=0,thread=0,status=0,phase=0;char op='?';uint64_t start=0,end=0,order=0,target=0;uint32_t mask=0;Book book;array<int,6> q{};vector<vector<RouteLeg>> result;vector<vector<Seat>> seats;int count=0;};
Event readevent(const J& j){Event e;e.idx=I(j["index"]);e.thread=I(j["thread"]);e.start=j["start"].u64();e.end=j["end"].u64();e.status=I(j["status"]);auto op=j["op"].str();if(op.size()!=1)throw runtime_error("bad op");e.op=op[0];auto phase=j["phase"].str();e.phase=phase=="PREFILL"?0:phase=="MAIN"?1:phase=="QUIET"?2:-1;
 if(e.op=='Q'){const auto& q=j["request"].a;if(q.size()!=6)throw runtime_error("bad query");for(int i=0;i<6;++i)e.q[i]=I(q[i]);e.count=I(j["count"]);for(const auto& path:j["result"].a){vector<RouteLeg> v;for(const auto& l:path["legs"].a){if(l.a.size()!=6)throw runtime_error("bad result leg");RouteLeg r;for(int i=0;i<6;++i)r[i]=I(l[i]);v.push_back(r);}if(path.has("count")&&I(path["count"])!=int(v.size()))throw runtime_error("bad path count");e.result.push_back(move(v));}}
 else if(e.op=='B')e.book=readbook(j["request"]);
 else if(e.op=='E'||e.op=='R'){const auto& r=j["request"];e.order=e.target=r["order"].u64();e.mask=uint32_t(r["cells"].num());if(e.op=='E')e.book=readbook(r["replacement"]);}
 if((e.op=='B'||e.op=='E')&&e.status==0){e.order=j["order"].u64();for(const auto& row:j["seats"].a){vector<Seat> r;for(const auto& sj:row.a){if(sj.a.size()!=3)throw runtime_error("bad seat");r.push_back({I(sj[0]),I(sj[1]),I(sj[2])});}e.seats.push_back(move(r));}}
 return e;}
constexpr int OK=0,SOLD_OUT=1,INVALID=2,NOT_FOUND=3,WITHHELD=4;
constexpr int COUNTS[3]={24,104,450}, ROWS[3]={8,13,18}, CARS[3]={1,2,4};
const string LETTERS[3]={"ACF","ACDF","ABCDF"};
struct Resolved{int train,day,a,b;RouteLeg leg;};
struct Hold{int a,b;uint64_t order;int p,l;bool operator==(const Hold&)const=default;};
struct Key{int t,day,cls,seat;bool operator==(const Key&)const=default;bool operator<(const Key& x)const{return tie(t,day,cls,seat)<tie(x.t,x.day,x.cls,x.seat);}};
struct KeyHash{size_t operator()(const Key& x)const{return (size_t(x.t)*1315423911u+size_t(x.day*3+x.cls))*617u+size_t(x.seat);}};
struct Order{Book book;vector<Resolved> resolved;vector<vector<int>> seats;uint32_t active=0;};
using Holds=unordered_map<Key,vector<Hold>,KeyHash>;
struct BaseIndices{vector<vector<pair<int,int>>> at;vector<vector<uint64_t>> incoming;vector<unordered_map<int,int>> stops;unordered_map<int,unordered_map<int,vector<pair<int,int>>>> ending;unordered_map<int,array<vector<uint64_t>,4>> reach;};
class Model{
public:
 const World* world;shared_ptr<BaseIndices> index;bool management=false;
 unordered_map<uint64_t,Order> orders;Holds holds;
 const Holds* base_holds=nullptr;set<tuple<uint64_t,int,int>> skipped;
 explicit Model(const World& w,bool m=false):world(&w),index(make_shared<BaseIndices>()),management(m){index->at.resize(w.stations);index->incoming.assign(w.stations,vector<uint64_t>((w.stations+63)/64));index->stops.resize(w.trains.size());for(int t=0;t<int(w.trains.size());t++){unordered_set<int> prev;for(int i=0;i<int(w.trains[t].size());++i){auto station=w.trains[t][i].station;index->at[station].push_back({t,i});index->stops[t][station]=i;for(int source:prev)index->incoming[station][source/64]|=1ull<<(source%64);prev.insert(station);}}}
 Model(const World* w,shared_ptr<BaseIndices> ix,bool m):world(w),index(move(ix)),management(m){}
 static Model inventory_overlay(const Model& initial){
  // Only the timetable is shared; base holds are read-only for this audit.
  Model out(initial.world,initial.index,initial.management);out.base_holds=&initial.holds;return out;
 }
 optional<vector<Resolved>> validate(const Book& b)const{if(b.legs.empty()||b.legs.size()>3||b.pax<1||b.pax>5||b.cls<0||b.cls>2)return nullopt;vector<Resolved> v;set<int> stations;set<pair<int,int>> used;stations.insert(b.legs[0][2]);for(const auto& leg:b.legs){auto [t,d,a,z]=leg;if(t<0||t>=int(world->trains.size())||d<0||d>=3)return nullopt;auto& lookup=index->stops[t];auto i=lookup.find(a),j=lookup.find(z);if(i==lookup.end()||j==lookup.end()||i->second>=j->second)return nullopt;if(!stations.insert(z).second||!used.insert({t,d}).second)return nullopt;const auto& st=world->trains[t];RouteLeg actual={t,d,a,z,d*1440+st[i->second].depart,d*1440+st[j->second].arrive};if(!v.empty()&&(v.back().leg[3]!=a||v.back().leg[5]+30>actual[4]))return nullopt;v.push_back({t,d,i->second,j->second,actual});}return v;}
 static int seatindex(int cls,const Seat& x){if(cls<0||cls>2||x[0]<CARS[cls]||x[0]>=CARS[cls]+(cls==0?1:cls==1?2:5)||x[1]<1||x[1]>ROWS[cls])return -1;auto col=LETTERS[cls].find(char(x[2]));if(col==string::npos)return -1;return ((x[0]-CARS[cls])*ROWS[cls]+x[1]-1)*int(LETTERS[cls].size())+int(col);}
 static uint32_t cells(int pax,int legs){uint32_t mask=0;for(int p=0;p<pax;p++)for(int l=0;l<legs;l++)mask|=1u<<(p*3+l);return mask;}
 bool free(const Resolved& r,int cls,int seat,const set<tuple<uint64_t,int,int>>& ignore={})const{
  const Key key{r.train,r.day,cls,seat};
  auto busy=[&](const vector<Hold>& items){for(const auto& h:items){auto cell=make_tuple(h.order,h.p,h.l);
    if(h.a<r.b&&r.a<h.b&&!ignore.count(cell)&&!skipped.count(cell))return true;}
    return false;};
  if(base_holds){auto it=base_holds->find(key);if(it!=base_holds->end()&&busy(it->second))return false;}
  auto it=holds.find(key);return it==holds.end()||!busy(it->second);
 }
 bool available(const Resolved& r,int cls,int pax,const set<tuple<uint64_t,int,int>>& ignore={})const{int n=0;for(int s=0;s<COUNTS[cls];++s)if(free(r,cls,s,ignore)&&++n>=pax)return true;return false;}
 static bool rankless(const vector<RouteLeg>& a,const vector<RouteLeg>& b){if(a.size()!=b.size())return a.size()<b.size();if(a.back()[5]!=b.back()[5])return a.back()[5]<b.back()[5];if(a.front()[4]!=b.front()[4])return a.front()[4]>b.front()[4];for(size_t i=0;i<a.size();i++)if(a[i][0]!=b[i][0]||a[i][1]!=b[i][1])return tie(a[i][0],a[i][1])<tie(b[i][0],b[i][1]);return false;}
 pair<int,vector<vector<RouteLeg>>> query(const array<int,6>& q)const{
 auto [origin,destination,earliest,k,cls,pax]=q;if(origin<0||destination<0||origin>=world->stations||destination>=world->stations||origin==destination||earliest<0||earliest>=4320||k<1||k>5||cls<0||cls>2||pax<1||pax>5)return {INVALID,{}};
 auto& ix=*index;
 auto rc=ix.reach.find(destination);
  if(rc==ix.reach.end()){
   array<vector<uint64_t>,4> reach;const size_t words=size_t((world->stations+63)/64);
   for(auto& v:reach)v.assign(words,0);
   reach[0][destination/64]|=1ull<<(destination%64);
   for(int depth=1;depth<4;++depth){
    for(size_t word=0;word<words;++word){uint64_t bits=reach[depth-1][word];
     while(bits){size_t target=word*64+size_t(countr_zero(bits));bits&=bits-1;
      const auto& incoming=ix.incoming[target];for(size_t j=0;j<words;++j)reach[depth][j]|=incoming[j];}
    }
   }
   rc=ix.reach.emplace(destination,move(reach)).first;
  }
  auto reachable=[&](int depth,int station){return bool(rc->second[depth][station/64]&(1ull<<(station%64)));};
 auto ec=ix.ending.find(destination);if(ec==ix.ending.end()){unordered_map<int,vector<pair<int,int>>> ending;for(auto [t,j]:ix.at[destination])for(int i=0;i<j;i++)ending[world->trains[t][i].station].push_back({t,i});ec=ix.ending.emplace(destination,move(ending)).first;}
 map<vector<pair<int,int>>,vector<RouteLeg>> found;int end=(earliest/1440+1)*1440,limit=numeric_limits<int>::max();
 auto update=[&](){if(int(found.size())<k){limit=numeric_limits<int>::max();return;}auto worst=max_element(found.begin(),found.end(),[](auto& a,auto& b){return rankless(a.second,b.second);});limit=worst->second.back()[5];};
 function<void(vector<RouteLeg>&,int,int,const set<int>&,int)> visit=[&](vector<RouteLeg>& path,int station,int ready,const set<int>& transfers,int length){int remaining=length-int(path.size());if(!reachable(remaining,station)||ready>=limit)return;bool last=int(path.size())+1==length;static const vector<pair<int,int>> no_edges;auto it=ec->second.find(station);const auto& edges=last?(it==ec->second.end()?no_edges:it->second):ix.at[station];for(auto [t,i]:edges){const auto& stops=world->trains[t];for(int day=0;day<3;day++){int depart=day*1440+stops[i].depart;if(depart<ready||depart>=limit||(path.empty()&&depart>=end))continue;bool dup=false;for(auto& leg:path)if(leg[0]==t&&leg[1]==day){dup=true;break;}if(dup)continue;int j0=i+1,j1=int(stops.size());if(last){auto it=ix.stops[t].find(destination);if(it==ix.stops[t].end())continue;j0=it->second;j1=j0+1;}for(int j=j0;j<j1;j++){auto [at,arrive,_]=stops[j];if((at==destination)!=last||at==origin||transfers.count(at)||!reachable(remaining-1,at)||day*1440+arrive>limit)continue;Resolved r{t,day,i,j,{t,day,station,at,depart,day*1440+arrive}};if(!available(r,cls,pax))continue;path.push_back(r.leg);if(at==destination){vector<pair<int,int>> key;for(auto& l:path)key.push_back({l[0],l[1]});auto it=found.find(key);auto arrival_less=[&](auto& a,auto& b){for(size_t z=0;z+1<a.size();z++)if(a[z][5]!=b[z][5])return a[z][5]<b[z][5];return false;};if(it==found.end()||arrival_less(path,it->second))found[key]=path;if(int(found.size())>k){auto worst=max_element(found.begin(),found.end(),[](auto& a,auto& b){return rankless(a.second,b.second);});found.erase(worst);}update();}else if(int(path.size())<length){auto next=transfers;next.insert(at);visit(path,at,r.leg[5]+30,next,length);}path.pop_back();}}}};
 for(int length=1;length<=3;length++){vector<RouteLeg> p;visit(p,origin,earliest,{},length);if(int(found.size())>=k)break;}vector<vector<RouteLeg>> out;for(auto& x:found)out.push_back(x.second);sort(out.begin(),out.end(),rankless);if(out.size()>size_t(k))out.resize(k);return {OK,move(out)};
 }
 void remove(uint64_t oid,uint32_t mask){auto& o=orders.at(oid);for(int p=0;p<o.book.pax;p++)for(int l=0;l<int(o.resolved.size());l++)if(mask&(1u<<(p*3+l))){auto r=o.resolved[l];Key key{r.train,r.day,o.book.cls,o.seats[l][p]};auto& hs=holds[key];erase_if(hs,[&](const Hold& h){return h.order==oid&&h.p==p&&h.l==l;});}o.active&=~mask;}
 bool apply(const Event& e){
  if(e.op=='Q'){auto [status,expected]=query(e.q);return e.status==status&&(status!=OK||(e.count==int(e.result.size())&&expected==e.result));}
  const Book& b=e.book;
  auto resolved=validate(b);
  const Order* old=nullptr;
  set<tuple<uint64_t,int,int>> ignore;
  if(e.op=='E'||e.op=='R'){
    uint32_t mask=e.mask;
    if(!mask||mask>=32768)return e.status==INVALID;
    if(e.op=='E'){
      int selected=0;for(int p=0;p<5;p++)selected+=(mask&(7u<<(p*3)))!=0;
      if(!resolved||selected!=b.pax)return e.status==INVALID;
    }
    auto it=orders.find(e.target);
    if(it==orders.end())return e.status==NOT_FOUND;
    old=&it->second;
    if(mask&~cells(old->book.pax,int(old->resolved.size())))return e.status==INVALID;
    if((mask&old->active)!=mask)return e.status==NOT_FOUND;
    if(e.op=='R'){
      if(e.status!=OK)return false;
      remove(e.target,mask);return true;
    }
    for(int p=0;p<5;p++)for(int l=0;l<3;l++)if(mask&(1u<<(p*3+l)))ignore.insert({e.target,p,l});
  }else if(!resolved)return e.status==INVALID;
  if(!resolved)return e.status==INVALID;
  for(auto& r:*resolved)if(!available(r,b.cls,b.pax,ignore))return e.status==SOLD_OUT;
  if(e.status==WITHHELD)return management;
  if(e.status!=OK||e.order==0||orders.count(e.order))return false;
  if(e.seats.size()!=resolved->size())return false;
  Order next;next.book=b;next.resolved=*resolved;next.active=cells(b.pax,int(resolved->size()));
  for(size_t l=0;l<resolved->size();l++){
    if(e.seats[l].size()!=size_t(b.pax))return false;
    set<int> unique;vector<int> seats;
    for(auto& x:e.seats[l]){
      int seat=seatindex(b.cls,x);
      if(seat<0||!unique.insert(seat).second||!free((*resolved)[l],b.cls,seat,ignore))return false;
      seats.push_back(seat);
    }next.seats.push_back(move(seats));
  }
  if(old)remove(e.target,e.mask);
  orders.emplace(e.order,next);
  for(size_t l=0;l<resolved->size();l++)for(int p=0;p<b.pax;p++){
    auto r=(*resolved)[l];Key key{r.train,r.day,b.cls,next.seats[l][p]};
    holds[key].push_back({r.a,r.b,e.order,p,int(l)});
  }
  return true;
 }
};
struct Error{string kind;int event=-1;vector<uint64_t> orders;vector<int> events;};
struct Coverage{int samples=0;size_t lifetimes=0;};
struct Cell {uint64_t oid;int p,l;bool operator<(const Cell& x)const{return tie(oid,p,l)<tie(x.oid,x.p,x.l);}};
struct Life {Key key;Hold hold;const Event* birth=nullptr;const Event* death=nullptr;};
struct AuditResult{Model model;vector<Error> errors;Coverage coverage;};
AuditResult audit(const Model& initial,const vector<const Event*>& events){
 auto allorders=initial.orders;
 map<uint64_t,const Event*> created;
 for(auto& [oid,_]:allorders)created[oid]=nullptr;
 map<Cell,const Event*> removed;
 vector<Error> errors;
 auto fail=[&](string kind,const Event& e){errors.push_back({move(kind),e.idx,{},{}});};
 for(auto ep:events){const auto& e=*ep;if(e.op!='B'&&e.op!='E')continue;auto resolved=initial.validate(e.book);
  if(!resolved){if(e.status!=INVALID)fail("invalid_booking",e);continue;}
  if(e.status==WITHHELD&&!initial.management)fail("withheld_in_performance",e);
  if(e.op=='B'&&(e.status==INVALID||e.status==NOT_FOUND))fail("booking_status",e);
  if(e.status!=OK)continue;
  if(!e.order||allorders.count(e.order)){fail("order_identity",e);continue;}
  if(e.seats.size()!=resolved->size()){fail("seat_shape",e);continue;}
  Order next;next.book=e.book;next.resolved=*resolved;next.active=Model::cells(e.book.pax,int(resolved->size()));bool valid=true;
  for(auto& row:e.seats){if(row.size()!=size_t(e.book.pax)){valid=false;break;}set<int> uniq;vector<int> seats;for(auto& s:row){int pos=Model::seatindex(e.book.cls,s);if(pos<0||!uniq.insert(pos).second){valid=false;break;}seats.push_back(pos);}next.seats.push_back(move(seats));}
  if(!valid){fail("invalid_seat",e);continue;}allorders.emplace(e.order,move(next));created[e.order]=ep;
 }
 for(auto ep:events){const auto& e=*ep;if(e.op!='R'&&e.op!='E')continue;
  int selected=0;for(int p=0;p<5;p++)selected+=(e.mask&(7u<<(p*3)))!=0;
  bool malformed=!e.mask||e.mask>=32768||(e.op=='E'&&(!initial.validate(e.book)||selected!=e.book.pax));
  if(malformed){if(e.status!=INVALID)fail("invalid_selection",e);continue;}
  auto it=allorders.find(e.target);auto ct=created.find(e.target);
  if(it==allorders.end()||(ct!=created.end()&&ct->second&&e.end<ct->second->start)){
   if(e.status!=NOT_FOUND)fail("unknown_order",e);
   continue;
  }
  const auto& order=it->second;
  if(e.mask&~Model::cells(order.book.pax,int(order.resolved.size()))){if(e.status!=INVALID)fail("selection_shape",e);continue;}
  if(e.status==INVALID||(e.op=='R'&&e.status!=OK&&e.status!=NOT_FOUND))fail("selection_status",e);
  if(e.status!=OK)continue;
  for(int p=0;p<order.book.pax;p++)for(int l=0;l<int(order.resolved.size());l++)if(e.mask&(1u<<(p*3+l))){Cell key{e.target,p,l};if(!(order.active&(1u<<(p*3+l)))||removed.count(key))fail("double_release",e);else removed[key]=ep;}
 }
 for(auto ep:events){const auto& e=*ep;if((e.op!='R'&&e.op!='E')||e.status!=NOT_FOUND)continue;
  auto it=allorders.find(e.target);if(it==allorders.end()||!e.mask||e.mask>=32768)continue;
  auto ct=created.find(e.target);const Event* birth=ct==created.end()?nullptr:ct->second;
  bool definitely_created=!birth||birth->end<e.start;auto& o=it->second;
  if(e.mask&~Model::cells(o.book.pax,int(o.resolved.size())))continue;
  bool missing=false;for(int p=0;p<o.book.pax;p++)for(int l=0;l<int(o.resolved.size());l++)if(e.mask&(1u<<(p*3+l))){auto rit=removed.find({e.target,p,l});if(!(o.active&(1u<<(p*3+l)))||(rit!=removed.end()&&rit->second->start<=e.end))missing=true;}
  if(definitely_created&&!missing)fail("false_not_found",e);
 }
 set<Cell> dying;for(auto& [key,death]:removed)if(created.at(key.oid)==nullptr)dying.insert(key);
 vector<Life> lifetimes;unordered_map<Key,vector<size_t>,KeyHash> byseat;
 set<uint64_t> dynamic;
 for(auto& [key,death]:removed)if(created.at(key.oid)==nullptr)dynamic.insert(key.oid);
 for(auto ep:events)if((ep->op=='B'||ep->op=='E')&&created.count(ep->order)&&created.at(ep->order)==ep)dynamic.insert(ep->order);
 for(uint64_t oid:dynamic){const auto& o=allorders.at(oid);const Event* birth=created.at(oid);
  for(int p=0;p<o.book.pax;p++)for(int l=0;l<int(o.resolved.size());l++){
   uint32_t bit=1u<<(p*3+l);if(!(o.active&bit))continue;auto it=removed.find({oid,p,l});const Event* death=it==removed.end()?nullptr:it->second;
   if(!birth&&!death)continue;
   auto r=o.resolved[l];Key k{r.train,r.day,o.book.cls,o.seats[l][p]};
   byseat[k].push_back(lifetimes.size());lifetimes.push_back({k,{r.a,r.b,oid,p,l},birth,death});
  }
 }
 // Certainty-interval oversell checks, including preexisting static holds.
 for(auto& [key,idxs]:byseat){struct H{Hold hold;int64_t start,end;};vector<H> holds;
  auto b=initial.holds.find(key);if(b!=initial.holds.end())for(const auto& h:b->second)if(!dying.count({h.order,h.p,h.l}))holds.push_back({h,-1,numeric_limits<int64_t>::max()});
  for(auto idx:idxs){const auto& l=lifetimes[idx];holds.push_back({l.hold,l.birth?int64_t(l.birth->end):-1,l.death?int64_t(l.death->start):numeric_limits<int64_t>::max()});}
  sort(holds.begin(),holds.end(),[](auto& a,auto& b){return a.start<b.start;});vector<H> active;
  for(const auto& h:holds){if(h.start>=h.end)continue;erase_if(active,[&](const H& x){return x.end<=h.start;});
   for(const auto& x:active)if(h.hold.a<x.hold.b&&x.hold.a<h.hold.b&&max(h.start,x.start)<min(h.end,x.end))errors.push_back({"oversell",-1,{h.hold.order,x.hold.order},{}});
   active.push_back(h);
  }
 }
 auto envelope=[&](const Event& e,bool worst,const set<Cell>& ignored=set<Cell>{},const set<tuple<int,int,int>>* restrict_resource=nullptr){
  // Avoid copying all existing inventory for every query envelope.
  Model result=Model::inventory_overlay(initial);
  for(const auto& c:dying)result.skipped.emplace(c.oid,c.p,c.l);
  for(const auto& c:ignored)result.skipped.emplace(c.oid,c.p,c.l);
  for(const auto& l:lifetimes){if(ignored.count({l.hold.order,l.hold.p,l.hold.l}))continue;if(restrict_resource&&!restrict_resource->count({l.key.t,l.key.day,l.key.cls}))continue;
   bool include=worst?((!l.birth||l.birth->start<=e.end)&&(!l.death||l.death->end>=e.start)):
      ((!l.birth||l.birth->end<e.start)&&(!l.death||l.death->start>e.end));
   if(include)result.holds[l.key].push_back(l.hold);
  }
  return result;
 };
 for(auto ep:events){const auto& e=*ep;if((e.op!='B'&&e.op!='E')||e.status!=SOLD_OUT)continue;auto rr=initial.validate(e.book);if(!rr)continue;
  set<Cell> ignored; if(e.op=='E')for(int p=0;p<5;p++)for(int l=0;l<3;l++)if(e.mask&(1u<<(p*3+l)))ignored.insert({e.target,p,l});
  set<tuple<int,int,int>> resources;for(auto& r:*rr)resources.insert({r.train,r.day,e.book.cls});auto possible=envelope(e,true,ignored,&resources);
  bool can=true;for(auto& r:*rr)if(!possible.available(r,e.book.cls,e.book.pax)){can=false;break;}if(can)fail("false_sold_out",e);
 }
 vector<const Event*> queries;for(auto ep:events)if(ep->op=='Q')queries.push_back(ep);
 set<size_t> selected;for(size_t i=0;i<min(size_t(64),queries.size());i++)selected.insert(i*queries.size()/min(size_t(64),queries.size()));
 for(size_t idx=0;idx<queries.size();idx++){const Event& e=*queries[idx];auto [a,z,earliest,k,cls,pax]=e.q;
  bool valid=a>=0&&z>=0&&a<initial.world->stations&&z<initial.world->stations&&a!=z&&earliest>=0&&earliest<4320&&k>=1&&k<=5&&cls>=0&&cls<3&&pax>=1&&pax<=5;
  if(!valid){if(e.status!=INVALID)fail("invalid_query",e);continue;}
  if(e.status!=OK){fail("query_status",e);continue;}
  if(e.count!=int(e.result.size())||e.result.size()>size_t(k)){fail("query_shape",e);continue;}
  set<tuple<int,int,int>> resources;
  for(auto& path:e.result)for(auto& leg:path)resources.insert({leg[0],leg[1],cls});
  auto optimistic=envelope(e,false,{},&resources);
  set<vector<pair<int,int>>> sequences;vector<vector<RouteLeg>> found;
  for(auto& path:e.result){Book b;b.cls=cls;b.pax=pax;for(auto& l:path)b.legs.push_back({l[0],l[1],l[2],l[3]});auto r=initial.validate(b);
   bool correct=!path.empty()&&r.has_value()&&r->size()==path.size()&&path.front()[2]==a&&path.back()[3]==z&&earliest<=path.front()[4]&&path.front()[4]<(earliest/1440+1)*1440;
   if(correct)for(size_t i=0;i<r->size();i++)if((*r)[i].leg!=path[i]){correct=false;break;}
   if(!correct){fail("query_route",e);continue;}
   bool possible=true;for(auto& x:*r)if(!optimistic.available(x,cls,pax)){possible=false;break;}
   if(!possible)fail("query_impossible_inventory",e);
   vector<pair<int,int>> seq;for(auto& l:path)seq.push_back({l[0],l[1]});if(!sequences.insert(seq).second)fail("query_order",e);
   found.push_back(path);
  }
  if(!is_sorted(found.begin(),found.end(),Model::rankless))fail("query_order",e);
  if(selected.count(idx)){
   auto guaranteed=envelope(e,true).query(e.q).second;
   for(auto& path:guaranteed){vector<pair<int,int>> seq;for(auto& leg:path)seq.push_back({leg[0],leg[1]});
    if(!sequences.count(seq)&&(int(found.size())<k||(!found.empty()&&Model::rankless(path,found.back()))))fail("query_definite_omission",e);
   }
  }
 }
 Model final=initial;final.orders=move(allorders);
 for(auto& key:dying){auto& o=final.orders.at(key.oid);auto r=o.resolved[key.l];Key k{r.train,r.day,o.book.cls,o.seats[key.l][key.p]};auto it=final.holds.find(k);if(it!=final.holds.end())erase_if(it->second,[&](const Hold& h){return h.order==key.oid&&h.p==key.p&&h.l==key.l;});}
 for(auto& [key,ep]:removed)final.orders.at(key.oid).active&=~(1u<<(key.p*3+key.l));
 for(const auto& l:lifetimes)if(!l.death)final.holds[l.key].push_back(l.hold);
 size_t livebefore=0;for(auto& [_,v]:initial.holds)livebefore+=v.size();
 return {move(final),move(errors),{int(selected.size()),livebefore-dying.size()+lifetimes.size()}};
}

struct Header{int threads=0,mode=0,calls=0;uint64_t init_ns=0,main_ns=0;};
Header readheader(const J& j){return {I(j["threads"]),I(j["mode"]),I(j["calls"]),j["init_ns"].u64(),j["main_ns"].u64()};}

struct Verdict{
 bool ok=false;vector<Error> errors;int exact=0,unresolved=0;vector<Coverage> coverage;
 double throughput=0;map<string,array<int64_t,5>> latency; // total, valid, successful, p50, p99
 map<string,double> quiet;map<string,double> quality;
};

optional<Model> linearize(const Model& initial,const vector<const Event*>& events){
 if(events.size()>8)throw runtime_error("bounded linearization requires <=8 calls");
 const int n=int(events.size());
 // Only successful writes to these keys can change the linearization state.
 // Memoizing the exact substate prevents exponential replay of identical
 // interleavings without hashing unrelated seats or relying on collisions.
 set<uint64_t> touched_orders;
 set<Key> touched_keys;
 auto note_order=[&](const Order& order){
  for(size_t l=0;l<order.resolved.size();++l){const auto& r=order.resolved[l];
   if(l>=order.seats.size())continue;
   for(int seat:order.seats[l])touched_keys.insert({r.train,r.day,order.book.cls,seat});
  }
 };
 for(const Event* e:events){
  if(e->op=='B'||e->op=='E'){
   if(e->status==OK){
    touched_orders.insert(e->order);
    auto r=initial.validate(e->book);
    if(r&&e->seats.size()==r->size())for(size_t l=0;l<r->size();++l)
     for(const auto& seat:e->seats[l]){
      int index=Model::seatindex(e->book.cls,seat);
      if(index>=0)touched_keys.insert({(*r)[l].train,(*r)[l].day,e->book.cls,index});
     }
   }
  }
  if(e->op=='R'||e->op=='E'){
   touched_orders.insert(e->target);
   auto it=initial.orders.find(e->target);
   if(it!=initial.orders.end())note_order(it->second);
  }
 }
 // Mutations of newly created orders are captured by the booking keys above.
 unordered_set<string> visited;
 auto signature=[&](const Model& m,uint32_t done){
  string key;
  auto append=[&](uint64_t u){for(int i=0;i<8;i++)key.push_back(char((u>>(i*8))&255));};
  append(done);
  for(uint64_t id:touched_orders){auto it=m.orders.find(id);append(it==m.orders.end()?UINT64_MAX:it->second.active);}
  for(const Key& seat:touched_keys){auto it=m.holds.find(seat);
   if(it==m.holds.end()){append(0);continue;}
   vector<tuple<int,int,uint64_t,int,int>> values;
   for(const Hold& h:it->second)values.emplace_back(h.a,h.b,h.order,h.p,h.l);
   sort(values.begin(),values.end());append(values.size());
   for(auto [a,b,oid,p,l]:values){append(uint32_t(a));append(uint32_t(b));append(oid);append(uint32_t(p));append(uint32_t(l));}
  }
  return key;
 };
 array<uint32_t,8> predecessors{};
 for(int i=0;i<n;i++)for(int j=0;j<n;j++)if(i!=j){const auto &e=*events[i],&other=*events[j];
  if(other.end<e.start || (other.thread==e.thread&&other.idx<e.idx))predecessors[i]|=1u<<j;
 }
 // The full model is cloned only along legal candidates. Timetable caches are shared.
 function<optional<Model>(const Model&,uint32_t)> dfs=[&](const Model& state,uint32_t done)->optional<Model>{
  if(done==(1u<<n)-1)return state;
  if(!visited.insert(signature(state,done)).second)return nullopt;
  for(int i=0;i<n;i++)if(!(done>>i&1)&&!(predecessors[i]&~done)){
   Model candidate=state;
   if(candidate.apply(*events[i])){
    auto next=dfs(candidate,done|(1u<<i));if(next)return next;
   }
  }
  return nullopt;
 };
 return dfs(initial,0);
}

int64_t percentile(vector<int64_t> v,double p){if(v.empty())return -1;const size_t k=max(size_t(1),size_t(ceil(v.size()*p)))-1;nth_element(v.begin(),v.begin()+k,v.end());return v[k];}

Verdict check(const World& world,const Header& h,const vector<Event>& events){
 if(h.calls!=int(events.size())||h.threads<1||h.threads>256||h.mode<0||h.mode>1)throw runtime_error("invalid ABI 2 log header/count");
 vector<unsigned char> seen(events.size(),0);
 for(const auto& e:events){
  if(e.idx<0||size_t(e.idx)>=events.size()||seen[e.idx]++)throw runtime_error("missing/duplicate event index");
  if(e.start>e.end||e.status<0||e.status>4||e.phase<0||e.phase>2||string("QBER").find(e.op)==string::npos)throw runtime_error("bad event window/phase/op/status");
 }
 Verdict result; if(h.init_ns>30000000000ull){result.errors.push_back({"init exceeds 30 seconds",-1,{},{}});return result;}
 Model model(world,h.mode==1);
 vector<const Event*> chronological;chronological.reserve(events.size());for(auto& e:events)chronological.push_back(&e);
 sort(chronological.begin(),chronological.end(),[](auto a,auto b){return tie(a->start,a->end,a->idx)<tie(b->start,b->end,b->idx);});
 vector<vector<const Event*>> groups;groups.reserve(events.size()/4+10);vector<const Event*> group;uint64_t end=0;
 auto flush=[&](){if(!group.empty()){groups.push_back(move(group));group.clear();}end=0;};
 for(auto e:chronological){
  if(h.threads==1||e->phase!=1){flush();groups.push_back({e});continue;}
  if(!group.empty()&&e->start>end)flush();
  group.push_back(e);end=max(end,e->end);
 }
 flush();
 for(const auto& g:groups){
  if(g.size()==1){if(!model.apply(*g.front())){result.errors.push_back({"sequential_model",-1,{}, {g.front()->idx}});break;}result.exact++;}
  else if(g.size()<=8 && all_of(g.begin(),g.end(),[](const Event* e){return e->op=='Q';})){
   // Overlapping read-only queries see the same immutable state. Avoid
   // deep-copying inventory for an exponential search that cannot change it.
   bool valid=true;for(const Event* e:g){if(!model.apply(*e)){valid=false;break;}}
   if(!valid){Error err;err.kind="linearizability";for(const Event* e:g)err.events.push_back(e->idx);result.errors.push_back(move(err));break;}
   result.exact+=int(g.size());
  }
  else if(g.size()<=8){auto next=linearize(model,g);if(!next){Error err;err.kind="linearizability";for(auto e:g)err.events.push_back(e->idx);result.errors.push_back(move(err));break;}model=move(*next);result.exact+=int(g.size());}
  else{result.unresolved+=int(g.size());auto x=audit(model,g);model=move(x.model);result.coverage.push_back(x.coverage);result.errors.insert(result.errors.end(),x.errors.begin(),x.errors.end());if(!x.errors.empty())break;}
 }
 struct Stat{vector<int64_t> durations;int64_t calls=0,success=0;};
 map<char,Stat> stats;
 int64_t maincount=0,eligible=0,accepted=0;
 for(const auto& e:events){if(e.phase!=1)continue;maincount++;
  auto& st=stats[e.op];st.calls++;
  bool valid=e.status!=INVALID && ((e.op!='R'&&e.op!='E') || (e.target>0&&e.mask>0));
  if(valid){st.durations.push_back(int64_t(e.end-e.start));if(e.status==OK)st.success++;}
  if((e.op=='B'||e.op=='E')&&(e.status==OK||e.status==SOLD_OUT)){eligible++;if(e.status==OK)accepted++;}
 }
 const pair<char,string> ops[]={{'Q',"query"},{'B',"book"},{'E',"exchange"},{'R',"refund"}};
 for(auto& [op,name]:ops){auto& s=stats[op];result.latency[name]={s.calls,int64_t(s.durations.size()),s.success,percentile(s.durations,.5),percentile(s.durations,.99)};}
 result.throughput=double(maincount)*1e9/double(max(uint64_t(1),h.main_ns));
 int64_t wantwin=0,gotwin=0,wanttogether=0,gottogether=0;
 for(const auto& e:events)if(e.phase==2&&(e.op=='B'||e.op=='E')&&e.status==OK){
  const auto& b=e.book;
  if(b.cls<0||b.cls>=3)continue;
  for(const auto& seats:e.seats){
   for(size_t p=0;p<seats.size();p++)if(b.window>>p&1){wantwin++;gotwin+=(seats[p][2]==65||seats[p][2]==70);}
   if(!b.together||b.pax<2)continue;
   wanttogether++;
   if(seats.empty())continue;
   bool samecar=all_of(seats.begin(),seats.end(),[&](auto& x){return x[0]==seats[0][0];});
   bool valid=all_of(seats.begin(),seats.end(),[&](auto& x){return LETTERS[b.cls].find(char(x[2]))!=string::npos;});
   if(!samecar||!valid)continue;
   int lo=INT32_MAX,hi=INT32_MIN;set<int> rows;vector<int> cols;
   for(auto& x:seats){lo=min(lo,x[1]);hi=max(hi,x[1]);rows.insert(x[1]);cols.push_back(int(LETTERS[b.cls].find(char(x[2]))));}
   const int width=int(LETTERS[b.cls].size()),block=(width+1)/2;
   bool satisfied=false;
   if(b.pax>width)satisfied=hi-lo<=1;
   else if(rows.size()!=1)satisfied=false;
   else if(b.pax>block)satisfied=true;
   else{int aisle=(b.cls==0?1:b.cls==1?2:3);bool side=cols[0]<aisle;
    satisfied=all_of(cols.begin(),cols.end(),[&](int x){return (x<aisle)==side;}) && *max_element(cols.begin(),cols.end())-*min_element(cols.begin(),cols.end())==b.pax-1;
   }
   gottogether+=satisfied;
  }
 }
 result.quiet={{"window_wanted",double(wantwin)},{"window_got",double(gotwin)},{"window_rate",wantwin?double(gotwin)/wantwin:1.0},{"together_wanted",double(wanttogether)},{"together_got",double(gottogether)},{"together_rate",wanttogether?double(gottogether)/wanttogether:1.0}};
 result.quality={{"eligible_trades",double(eligible)},{"accepted_trades",double(accepted)},{"satisfaction",eligible?double(accepted)/eligible:1.0}};
 result.ok=result.errors.empty();return result;
}

string verdict_json(const Verdict& v){
 ostringstream s;s<<setprecision(17);s<<"{\"schema_version\":2,\"ok\":"<<(v.ok?"true":"false")<<",\"errors\":[";
 for(size_t i=0;i<v.errors.size();i++){if(i)s<<',';const auto& e=v.errors[i];
  if(e.kind=="init exceeds 30 seconds"){s<<escape(e.kind);continue;}
  s<<"{\"kind\":"<<escape(e.kind);
  if(e.event>=0)s<<",\"event\":"<<e.event;
  if(!e.events.empty()){s<<",\"events\":[";for(size_t j=0;j<e.events.size();j++){if(j)s<<',';s<<e.events[j];}s<<']';}
  if(!e.orders.empty()){s<<",\"orders\":[";for(size_t j=0;j<e.orders.size();j++){if(j)s<<',';s<<e.orders[j];}s<<']';}
  s<<'}';
 }
 s<<"],\"exact_calls\":"<<v.exact<<",\"unresolved_calls\":"<<v.unresolved
  <<",\"complete_linearizability_proof\":"<<(v.unresolved==0&&v.errors.empty()?"true":"false")
  <<",\"large_inventory_audit_complete\":"<<(v.errors.empty()?"true":"false")<<",\"inventory_audits\":[";
 for(size_t i=0;i<v.coverage.size();i++){if(i)s<<',';s<<"{\"query_envelope_samples\":"<<v.coverage[i].samples<<",\"lifetimes\":"<<v.coverage[i].lifetimes<<'}';}
 s<<"],\"throughput\":"<<v.throughput<<",\"latency\":{";
 size_t j=0;for(auto& [name,vv]:v.latency){if(j++)s<<',';s<<escape(name)<<":{\"count\":"<<vv[1]<<",\"p50_ns\":";
  if(vv[3]<0)s<<"null";else s<<vv[3];s<<",\"p99_ns\":";if(vv[4]<0)s<<"null";else s<<vv[4];
  s<<",\"sample_sufficient\":"<<(vv[1]>=10000?"true":"false")<<",\"total_calls\":"<<vv[0]<<",\"successful_calls\":"<<vv[2]<<'}';}
 s<<"},\"quiet\":{";j=0;for(auto& [name,value]:v.quiet){if(j++)s<<',';s<<escape(name)<<':'<<value;}
 s<<"},\"quality\":{";j=0;for(auto& [name,value]:v.quality){if(j++)s<<',';s<<escape(name)<<':'<<value;}
 s<<"}}";return s.str();
}
int main(int argc,char** argv){
 try{
  if(argc!=5||string(argv[3])!="--out")throw runtime_error("usage: railv2_judge WORLD TRACE.jsonl --out RESULT.json");
  World w=world_read(argv[1]);ifstream f(argv[2]);if(!f)throw runtime_error("cannot read trace");
  string line;if(!getline(f,line))throw runtime_error("empty trace");Header h=readheader(json(line));
  if(h.calls<0||h.calls>10000000)throw runtime_error("invalid calls count");
  vector<Event> events;events.reserve(size_t(h.calls));
  while(getline(f,line)){if(line.empty())throw runtime_error("empty JSONL line");events.push_back(readevent(json(line)));}
  auto result=check(w,h,events);ofstream o(argv[4]);if(!o)throw runtime_error("cannot write result");o<<verdict_json(result)<<'\n';if(!o)throw runtime_error("result write failed");
  return result.ok?0:1;
 }catch(const exception& e){cerr<<"railv2_judge: "<<e.what()<<'\n';return 2;}
}